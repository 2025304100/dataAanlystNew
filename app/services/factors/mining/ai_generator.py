"""AI 骨架生成（需求 §3.3 / 向导 §6.3.9；任务 T22）。

**批量口径（以需求 §3.3 为准）**
==============================
单次挖掘 **10 次调用**，每次批量返回 **8~12 个骨架**（合计约 80~120），
经校验 / 来源内去重 / 跨来源去重后保留约 57 个。

| 项 | 值 |
|---|---|
| 调用上限 | 10 次 |
| 并发 | **4 路**（`asyncio.to_thread` + `ThreadPoolExecutor`） |
| 单次超时 | 30 秒 |
| **AI 环节总超时** | **5 分钟**（超出 → 剩余名额全部回退随机） |
| **成本上限** | **¥10 / 500k token**（达上限停止调用） |
| 失败 / 超时 / 校验不通过 | 名额回退**受约束随机**补位 |

> **最坏情况**：10×8=80 个叠满三层损耗（校验 15% + 来源内去重 20% + 跨来源 10%）
> 后仅剩约 **49 < 57**，由随机补足 —— **属预期行为，不是缺陷**（需求 §3.3 明说）。
> 故本模块**如实返回 `shortfall`**，不做「凑数」。

**强制输出四字段（需求 §3.3）**：`formula_ast` / `category` / `economic_logic` /
`expected_direction` —— **缺任一即丢弃该骨架**、名额回退随机（M1 验收：缺逻辑的不入库）。

**自由度约束盒子（向导 §6.3.9）**：约束**同时作用于两层** ——
① Prompt 层（写进系统提示词，并声明「违反约束的公式将被丢弃」）
② 系统校验层（输出后逐项强制校验，不过即丢弃并记录原因）。
提供 3 个预设（保守 3/1/1、平衡 5/2/2、激进 8/3/4）+ 自定义微调。

设计要点（与任务卡三个坑对应）
============================
1. **`call_llm_with_failover` 是同步 `def`** → 必须在线程里调；本模块用
   `asyncio.to_thread`（其实现即 `loop.run_in_executor(None, ...)`），
   并**显式建 `ThreadPoolExecutor`** 以固定并发上限与关闭时机。
   ⚠️ 绝不直接 `await` 同步函数（会阻塞事件循环）。
2. **并发 4 路用 `ThreadPoolExecutor`**，不用 `ProcessPool` ——
   LLM 调用是 I/O 等待型，进程池反而多一层序列化开销且拿不到 DB session。
3. **AI 全失败时进化仍要能跑完**：本模块**永不抛异常**，失败只体现为
   `shortfall > 0`，由调用方（T23）用受约束随机补足。
4. **不改 `llm_client`**（任务卡「明确不做」）：调用方通过 `llm_caller` 注入，
   生产环境传 `llm_client.call_llm_with_failover`，测试传假实现（**离线可测**）。
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 口径常量（需求 §3.3 / 向导 §6.3.9）
# ══════════════════════════════════════════════════════════

#: 单次挖掘的调用上限
MAX_CALLS = 10
#: 每次批量返回的骨架数范围
BATCH_MIN, BATCH_MAX = 8, 12
DEFAULT_BATCH_SIZE = 10
#: 并发路数
DEFAULT_CONCURRENCY = 4
#: 单次调用超时（秒）
CALL_TIMEOUT_S = 30.0
#: AI 环节总超时（秒）—— 超出则剩余名额全部回退随机
TOTAL_TIMEOUT_S = 300.0
#: 成本上限：token 数与人民币
TOKEN_BUDGET = 500_000
COST_BUDGET_CNY = 10.0
#: 由「¥10 / 500k token」折算的**保守预算单价**（非实际计价，仅用于封顶判断）
COST_PER_TOKEN_CNY = COST_BUDGET_CNY / TOKEN_BUDGET
#: 去重反馈重试：单个骨架最多重试次数（向导 §6.3.8 第 1 层）
MAX_RETRY_PER_SKELETON = 3
#: 默认温度（向导 §6.3.9）
DEFAULT_TEMPERATURE = 0.7
#: 类别配额：6 类各 3~4 个（向导 §6.3.8 第 1 层类型配额）
CATEGORY_QUOTA_MIN, CATEGORY_QUOTA_MAX = 3, 4

#: 创新程度
INNOV_CONSERVATIVE = "conservative"
INNOV_BALANCED = "balanced"
INNOV_AGGRESSIVE = "aggressive"
VALID_INNOVATION: tuple[str, ...] = (INNOV_CONSERVATIVE, INNOV_BALANCED,
                                     INNOV_AGGRESSIVE)

#: 类型严格度
TYPE_STRICT = "strict"
TYPE_LOOSE = "loose"
VALID_TYPE_STRICTNESS: tuple[str, ...] = (TYPE_STRICT, TYPE_LOOSE)

#: 6 类（与 `category.py` 一致，此处不 import 以保持本模块可独立加载）
ALL_CATEGORIES: tuple[str, ...] = ("trend", "reversal", "volatility",
                                   "valuation", "quality", "volume_price")

#: 禁止结构（向导 §6.3.9）
BANNABLE_STRUCTURES: tuple[str, ...] = ("divide", "log", "exp", "sqrt", "power")
BANNED_STRUCTURE_ZH: dict[str, str] = {
    "divide": "禁止除法（`/`）",
    "log": "禁止 `log`",
    "exp": "禁止 `exp`",
    "sqrt": "禁止 `sqrt`",
    "power": "禁止幂运算（`**`）",
}

#: 约束预设（向导 §6.3.9 三档）
FREEDOM_PRESETS: dict[str, dict[str, Any]] = {
    "conservative": {"max_operators": 3, "max_nesting": 1, "max_field_refs": 1,
                     "innovation": INNOV_CONSERVATIVE, "label_zh": "保守探索"},
    "balanced": {"max_operators": 5, "max_nesting": 2, "max_field_refs": 2,
                 "innovation": INNOV_BALANCED, "label_zh": "平衡模式（默认）"},
    "aggressive": {"max_operators": 8, "max_nesting": 3, "max_field_refs": 4,
                   "innovation": INNOV_AGGRESSIVE, "label_zh": "激进探索"},
}

#: 自定义参数边界（向导 §6.3.9）
LIMITS = {
    "max_operators": (3, 8),
    "max_nesting": (1, 3),
    "max_field_refs": (1, 4),
    "window_min": (1, 250),
    "window_max": (1, 250),
}

#: 丢弃原因码
DROP_MISSING_FIELDS = "missing_required_fields"
DROP_BAD_CATEGORY = "bad_category"
DROP_COMPILE_FAILED = "compile_failed"
DROP_CONSTRAINT = "constraint_violation"
DROP_DUPLICATE = "duplicate"
DROP_PARSE_FAILED = "parse_failed"
DROP_STRUCTURE_BANNED = "structure_banned"
DROP_TYPE_MISMATCH = "type_mismatch"
DROP_REASON_ZH: dict[str, str] = {
    DROP_MISSING_FIELDS: "缺少强制字段（formula_ast/category/economic_logic/expected_direction）",
    DROP_BAD_CATEGORY: "category 不在 6 类之内",
    DROP_COMPILE_FAILED: "公式未通过编译校验",
    DROP_CONSTRAINT: "违反自由度约束（算子数/嵌套/字段引用/窗口/算子白名单）",
    DROP_DUPLICATE: "与已有候选重复（formula_hash 相撞）",
    DROP_PARSE_FAILED: "返回内容无法解析为骨架列表",
    DROP_STRUCTURE_BANNED: "命中「禁止结构」",
    DROP_TYPE_MISMATCH: "类型不匹配（严格模式下同类型才能运算）",
}


# ══════════════════════════════════════════════════════════
# 自由度约束盒子
# ══════════════════════════════════════════════════════════


@dataclass
class FreedomBox:
    """AI 自由度约束（向导 §6.3.9 的「可调约束盒子」）。

    同一份约束要**同时**用于 Prompt 注入与系统校验，故这里集中承载，
    避免「提示词说一套、校验做另一套」。
    """

    max_operators: int = 5
    max_nesting: int = 2
    max_field_refs: int = 2
    window_min: int = 5
    window_max: int = 120
    allowed_functions: tuple[str, ...] | None = None   # None = 全部允许
    allowed_fields: tuple[str, ...] | None = None      # None = 用户已选字段
    innovation: str = INNOV_BALANCED
    type_strictness: str = TYPE_STRICT
    banned_structures: tuple[str, ...] = ()

    # ── 构造 ──

    @classmethod
    def from_preset(cls, name: str, **overrides: Any) -> "FreedomBox":
        """由 3 档预设构造（可再用 overrides 微调）。"""
        preset = FREEDOM_PRESETS.get(str(name).lower())
        if preset is None:
            raise ValueError(
                f"未知预设 {name!r}；可选 {sorted(FREEDOM_PRESETS)}。")
        payload = {k: v for k, v in preset.items() if k != "label_zh"}
        payload.update(overrides)
        return cls(**payload)

    def replace(self, **overrides: Any) -> "FreedomBox":
        data = self.to_dict()
        data.update(overrides)
        return FreedomBox(**data)

    # ── 校验 ──

    def validate(self) -> None:
        for name, (lo, hi) in LIMITS.items():
            value = getattr(self, name)
            if not (lo <= int(value) <= hi):
                raise ValueError(f"{name}={value} 超出允许范围 [{lo}, {hi}]。")
        if self.window_min > self.window_max:
            raise ValueError(
                f"window_min({self.window_min}) 不能大于 window_max({self.window_max})。")
        if self.innovation not in VALID_INNOVATION:
            raise ValueError(
                f"innovation 必须是 {list(VALID_INNOVATION)} 之一。")
        if self.type_strictness not in VALID_TYPE_STRICTNESS:
            raise ValueError(
                f"type_strictness 必须是 {list(VALID_TYPE_STRICTNESS)} 之一。")
        unknown = sorted(set(self.banned_structures) - set(BANNABLE_STRUCTURES))
        if unknown:
            raise ValueError(
                f"未知「禁止结构」{unknown}；可选 {list(BANNABLE_STRUCTURES)}。")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_operators": self.max_operators,
            "max_nesting": self.max_nesting,
            "max_field_refs": self.max_field_refs,
            "window_min": self.window_min,
            "window_max": self.window_max,
            "allowed_functions": (list(self.allowed_functions)
                                  if self.allowed_functions is not None else None),
            "allowed_fields": (list(self.allowed_fields)
                               if self.allowed_fields is not None else None),
            "innovation": self.innovation,
            "type_strictness": self.type_strictness,
            "banned_structures": list(self.banned_structures),
        }

    def prompt_lines(self) -> list[str]:
        """→ 注入系统提示词的约束条目（**与校验同源**，务必成对维护）。"""
        lines = [
            f"- 算子数量 ≤ {self.max_operators}",
            f"- 嵌套深度 ≤ {self.max_nesting}",
            f"- 不同基础字段引用数 ≤ {self.max_field_refs}",
            f"- 窗口期取值在 [{self.window_min}, {self.window_max}] 内",
        ]
        if self.allowed_functions is not None:
            lines.append(f"- 只允许使用这些算子：{', '.join(self.allowed_functions)}")
        if self.allowed_fields is not None:
            lines.append(f"- 只允许引用这些字段：{', '.join(self.allowed_fields)}")
        if self.banned_structures:
            banned = "；".join(BANNED_STRUCTURE_ZH.get(s, s)
                              for s in self.banned_structures)
            lines.append(f"- 禁止结构：{banned}")
        lines.append(
            f"- 类型严格度：{'严格（同类型字段才能加减，截面算子输出无量纲）' if self.type_strictness == TYPE_STRICT else '宽松（允许跨类型运算）'}")
        lines.append(f"- 创新程度：{self.innovation}")
        lines.append("**违反以上任何约束的公式将被系统直接丢弃。**")
        return lines


# ══════════════════════════════════════════════════════════
# 配置与结果
# ══════════════════════════════════════════════════════════


@dataclass
class AIGeneratorConfig:
    """AI 生成配置（含预算、并发、约束）。"""

    target_count: int
    freedom: FreedomBox = field(default_factory=FreedomBox)
    selected_fields: tuple[str, ...] = ()
    enabled_categories: tuple[str, ...] = ALL_CATEGORIES
    #: Few-shot 示例（经典模板公式）
    few_shot_formulas: tuple[str, ...] = ()
    #: 已有候选公式（经典 + 已生成 AI）→ Prompt 注入 + 哈希去重
    existing_formulas: tuple[str, ...] = ()
    max_calls: int = MAX_CALLS
    batch_size: int = DEFAULT_BATCH_SIZE
    concurrency: int = DEFAULT_CONCURRENCY
    call_timeout_s: float = CALL_TIMEOUT_S
    total_timeout_s: float = TOTAL_TIMEOUT_S
    token_budget: int = TOKEN_BUDGET
    cost_budget_cny: float = COST_BUDGET_CNY
    temperature: float = DEFAULT_TEMPERATURE
    seed: int = 42

    def __post_init__(self) -> None:
        self.freedom.validate()
        if self.target_count < 0:
            raise ValueError("target_count 不能为负。")
        if not (BATCH_MIN <= int(self.batch_size) <= BATCH_MAX):
            raise ValueError(
                f"batch_size 必须在 [{BATCH_MIN}, {BATCH_MAX}]（需求 §3.3）。")
        if int(self.concurrency) < 1:
            raise ValueError("concurrency 至少为 1。")

    @property
    def planned_calls(self) -> int:
        """按目标名额反推需要几次调用（受调用上限约束）。"""
        if self.target_count <= 0:
            return 0
        per_call = max(1, self.batch_size)
        needed = (self.target_count + per_call - 1) // per_call
        return int(min(self.max_calls, max(1, needed)))

    @property
    def category_quota(self) -> dict[str, int]:
        """6 类配额（各 3~4，用于 Prompt 引导均衡覆盖）。"""
        cats = [c for c in self.enabled_categories if c in ALL_CATEGORIES]
        if not cats:
            return {}
        quota = {c: CATEGORY_QUOTA_MIN for c in cats}
        remaining = max(0, self.target_count - CATEGORY_QUOTA_MIN * len(cats))
        for cat in cats:
            if remaining <= 0:
                break
            add = min(remaining, CATEGORY_QUOTA_MAX - quota[cat])
            quota[cat] += add
            remaining -= add
        return quota


@dataclass
class AIResult:
    """AI 生成结果。**永不抛异常**：失败只体现为 `shortfall`。"""

    candidates: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    drops: list[dict[str, Any]] = field(default_factory=list)
    shortfall: int = 0
    notes_zh: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"candidates": self.candidates, "stats": dict(self.stats),
                "drops": self.drops, "shortfall": self.shortfall,
                "notes_zh": list(self.notes_zh)}


# ══════════════════════════════════════════════════════════
# Prompt 构造
# ══════════════════════════════════════════════════════════

SYSTEM_PROMPT_HEAD = """你是量化因子研究助手。请生成**候选因子公式骨架**，输出严格 JSON。

输出格式（**只输出 JSON，不要解释、不要 Markdown 围栏**）：
{"candidates": [
  {"formula_ast": "mean(close,20)/mean(close,60)-1",
   "category": "trend",
   "economic_logic": "短期均线上穿长期均线，捕捉趋势启动",
   "expected_direction": "positive"}
]}

硬性要求：
- 每个候选**必须**含上述 4 个字段，缺任一将被系统丢弃
- `category` 必须是以下之一：trend / reversal / volatility / valuation / quality / volume_price
- `expected_direction` 必须是 positive 或 negative
- `economic_logic` 用一句中文说明经济含义（不可为空）
"""


def _field_type_hint(selected_fields: Sequence[str]) -> list[str]:
    """把用户已选字段按类型分组给出提示（不做类型强制，仅帮助模型选字段）。"""
    from app.services.factors.mining.category import (  # 延迟 import：避免循环
        PRICE_FIELDS, QUALITY_FIELDS, VALUATION_FIELDS, VOLUME_PRICE_FIELDS,
    )

    groups = [
        ("价格", PRICE_FIELDS), ("量价", VOLUME_PRICE_FIELDS),
        ("估值", VALUATION_FIELDS), ("质量", QUALITY_FIELDS),
    ]
    out: list[str] = []
    for label, known in groups:
        hit = [f for f in selected_fields if f in known]
        if hit:
            out.append(f"  · {label}：{', '.join(hit)}")
    other = [f for f in selected_fields
             if f not in PRICE_FIELDS | VOLUME_PRICE_FIELDS | VALUATION_FIELDS | QUALITY_FIELDS]
    if other:
        out.append(f"  · 其它：{', '.join(other)}")
    return out


def build_prompt(cfg: AIGeneratorConfig, *, batch_index: int,
                 feedback: Sequence[str] = ()) -> list[dict[str, Any]]:
    """构造一次调用的 messages（系统约束 + Few-shot + 已存在公式 + 本批要求）。

    `feedback`：上一批因重复/违规被丢弃的公式说明（**去重反馈重试**，向导 §6.3.8）。
    """
    lines: list[str] = [SYSTEM_PROMPT_HEAD, "本次生成约束："]
    lines.extend(cfg.freedom.prompt_lines())
    if cfg.selected_fields:
        lines.append("- 只允许引用用户已选字段：")
        lines.extend(_field_type_hint(list(cfg.selected_fields)))
    quota = cfg.category_quota
    if quota:
        lines.append("- 类型配额（尽量均衡）："
                     + "；".join(f"{c} {n} 个" for c, n in quota.items()))

    if cfg.few_shot_formulas:
        lines.append("\n参考以下**经典因子模板**（请参考结构做创新，不要照抄）：")
        lines.extend(f"  - {f}" for f in list(cfg.few_shot_formulas)[:12])

    if cfg.existing_formulas:
        lines.append("\n以下公式**已存在**，请勿重复（含等价写法）：")
        lines.extend(f"  - {f}" for f in list(cfg.existing_formulas)[:60])

    if feedback:
        lines.append("\n上一批被丢弃的原因（请换结构，不要重复提交）：")
        lines.extend(f"  - {msg}" for msg in list(feedback)[:20])

    lines.append(
        f"\n请生成 {cfg.batch_size} 个骨架（第 {batch_index + 1} 批）。"
        "只输出 JSON。")
    return [
        {"role": "system", "content": "\n".join(lines[:3])},
        {"role": "user", "content": "\n".join(lines[3:])},
    ]


# ══════════════════════════════════════════════════════════
# 解析
# ══════════════════════════════════════════════════════════

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_ai_payload(raw: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """把模型返回解析成骨架列表 → `(items, 错误原因)`。

    容错：剥 Markdown 围栏、取首个 JSON 对象/数组、允许 `candidates` 包裹。
    解析失败**不抛**（返回空列表 + 原因），由调用方记为 `parse_failed`。
    """
    if not raw or not str(raw).strip():
        return [], "空响应"
    text = str(raw).strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        payload = json.loads(text)
    except ValueError:
        # 退化：截取首个 `{`..末个 `}`
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return [], "无法找到 JSON 对象"
        try:
            payload = json.loads(text[i:j + 1])
        except ValueError as exc:
            return [], f"JSON 解析失败：{exc}"

    if isinstance(payload, dict):
        items = payload.get("candidates") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return [], "顶层既不是对象也不是数组"
    if not isinstance(items, list):
        return [], "candidates 不是数组"
    return [x for x in items if isinstance(x, dict)], None


# ══════════════════════════════════════════════════════════
# 逐骨架校验（系统校验层）
# ══════════════════════════════════════════════════════════

_FUNC_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(")
_FIELD_RE = re.compile(r"(?<![A-Za-z_0-9])([a-z_][a-z_0-9]*)(?![A-Za-z_0-9(])")
_NUM_RE = re.compile(r"(?<![A-Za-z_0-9.])(\d+(?:\.\d+)?)(?![A-Za-z_0-9])")


def _nesting_depth(formula: str) -> int:
    depth = max_depth = 0
    for ch in formula:
        if ch == "(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch == ")":
            depth = max(0, depth - 1)
    return max(0, max_depth - 1) if max_depth else 0


def validate_skeleton(
    item: Mapping[str, Any], *, cfg: AIGeneratorConfig,
    compiler: Callable[..., Any] | None = None,
    known_fields: Iterable[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """逐骨架校验 → `(candidate, 丢弃原因码)`。

    校验顺序（任一不过即丢弃，**不静默降级**）：
    1. 强制四字段齐备 → 2. category 合法 → 3. 禁止结构 → 4. 算子白名单
    → 5. 复杂度（算子数/嵌套/字段引用）→ 6. 窗口范围 → 7. 编译校验
    """
    # ① 强制四字段（需求 §3.3：缺任一丢弃）
    formula = str(item.get("formula_ast") or item.get("formula") or "").strip()
    category = str(item.get("category") or "").strip()
    logic = str(item.get("economic_logic") or "").strip()
    direction = str(item.get("expected_direction") or "").strip().lower()
    if not formula or not category or not logic or not direction:
        return None, DROP_MISSING_FIELDS

    # ② category 合法
    if category not in ALL_CATEGORIES:
        return None, DROP_BAD_CATEGORY
    if category not in cfg.enabled_categories:
        return None, DROP_BAD_CATEGORY

    # ③ 禁止结构
    banned = set(cfg.freedom.banned_structures)
    if "divide" in banned and "/" in formula:
        return None, DROP_STRUCTURE_BANNED
    if "power" in banned and "**" in formula:
        return None, DROP_STRUCTURE_BANNED
    funcs = _FUNC_RE.findall(formula)
    for key in ("log", "exp", "sqrt"):
        if key in banned and key in funcs:
            return None, DROP_STRUCTURE_BANNED

    # ④ 算子白名单 + 复杂度
    if cfg.freedom.allowed_functions is not None:
        not_allowed = [f for f in funcs if f not in cfg.freedom.allowed_functions]
        if not_allowed:
            return None, DROP_CONSTRAINT
    if len(funcs) > cfg.freedom.max_operators:
        return None, DROP_CONSTRAINT
    if _nesting_depth(formula) > cfg.freedom.max_nesting:
        return None, DROP_CONSTRAINT

    # ⑤ 字段引用
    field_pool = set(cfg.freedom.allowed_fields or
                     cfg.selected_fields or (known_fields or ()))
    refs: list[str] = []
    for name in _FIELD_RE.findall(formula):
        if name in funcs:
            continue
        if field_pool and name not in field_pool:
            return None, DROP_CONSTRAINT          # 引用了未允许字段
        if field_pool and name not in refs:
            refs.append(name)
    if field_pool and len(refs) > cfg.freedom.max_field_refs:
        return None, DROP_CONSTRAINT

    # ⑥ 窗口范围（只检查纯数字参数中"像窗口"的那些：>0 的整数）
    for num_text in _NUM_RE.findall(formula):
        try:
            value = float(num_text)
        except ValueError:
            continue
        if value != int(value) or value <= 0:
            continue
        if value < cfg.freedom.window_min or value > cfg.freedom.window_max:
            # 常数（如 `-1`）不一定是窗口；只在明显是窗口参数时报错：
            # 判据 = 该数字作为函数调用的最后一个参数出现
            if re.search(rf",\s*{int(value)}\s*\)", formula):
                return None, DROP_CONSTRAINT

    # ⑦ 编译校验（AST/类型/依赖）
    if compiler is None:
        from app.services.factors.factor_compiler import compile_formula

        compiler = lambda *, formula, params=None: compile_formula(  # noqa: E731
            formula=formula, params=params)
    result = compiler(formula=formula, params=None)
    if not (getattr(result, "success", False)
            and getattr(result, "execution_plan", None) is not None):
        return None, DROP_COMPILE_FAILED
    plan = result.execution_plan

    candidate = {
        "formula": formula,
        "canonical_formula": getattr(plan, "formula", formula),
        "formula_ast": item.get("formula_ast"),
        "category": category,
        "economic_logic": logic,
        "expected_direction": direction,
        "interpretability_score": float(item.get("interpretability_score") or 0.0),
        "logic_source": "ai",
        "source": "ai",
        "operation": "ai_generated",
        "generation": 0,
        "required_fields": refs,
        "complexity": float(getattr(plan, "complexity_score", 0.0)),
        "node_count": int(getattr(plan, "node_count", 0)),
        "max_lookback": int(getattr(plan, "max_lookback", 1)),
        "operator_count": len(funcs),
        "nesting_depth": _nesting_depth(formula),
        "compile_ok": True,
    }
    return candidate, None


# ══════════════════════════════════════════════════════════
# 主入口（异步 + 线程池）
# ══════════════════════════════════════════════════════════


def _hash_of(formula: str) -> str:
    """复用 T19 的公式哈希（**单一实现**，R28）。"""
    from app.services.factors.mining.dedup import formula_hash

    return formula_hash(formula)


def generate_ai_candidates(
    cfg: AIGeneratorConfig, *,
    llm_caller: Callable[..., Any] | None = None,
    compiler: Callable[..., Any] | None = None,
    db: Any = None,
    profile_id: int | None = None,
) -> AIResult:
    """**同步入口**（内部用 asyncio + 线程池）。永不抛异常。

    Args:
        llm_caller: 注入的 LLM 调用（缺省用 `llm_client.call_llm_with_failover`）。
            **测试传假实现即可离线验证**（本模块不直连任何外部服务）。
        compiler: 可注入编译器（测试用假编译器免真实编译开销）。
        db: 传给 `llm_caller` 的 session（`call_llm_with_failover(db, messages, ...)`）。
    """
    if llm_caller is None:
        from app.services.ai.llm_client import call_llm_with_failover

        llm_caller = call_llm_with_failover
    try:
        return asyncio.run(_generate_async(cfg, llm_caller=llm_caller,
                                           compiler=compiler, db=db,
                                           profile_id=profile_id))
    except Exception as exc:  # noqa: BLE001 - 契约：本模块永不抛
        logger.exception("AI 生成整体失败，全部名额回退随机")
        return AIResult(
            shortfall=cfg.target_count,
            stats={"calls_made": 0, "accepted": 0, "target": cfg.target_count},
            notes_zh=[f"AI 生成整体失败（{type(exc).__name__}: {exc}），"
                      f"{cfg.target_count} 个名额全部回退受约束随机。"],
        )


async def _generate_async(
    cfg: AIGeneratorConfig, *, llm_caller: Callable[..., Any],
    compiler: Callable[..., Any] | None, db: Any, profile_id: int | None,
) -> AIResult:
    started = time.monotonic()
    planned = cfg.planned_calls
    stats: dict[str, Any] = {
        "target": cfg.target_count, "planned_calls": planned,
        "calls_made": 0, "calls_failed": 0, "accepted": 0,
        "tokens_used": 0, "cost_cny": 0.0,
        "elapsed_ms": 0, "stopped_reason": None,
    }
    drops: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    accepted_hashes: dict[str, str] = {}      # hash → formula
    for f in cfg.existing_formulas:
        accepted_hashes[_hash_of(str(f))] = str(f)

    feedback: list[str] = []
    lock = asyncio.Lock()
    stop_flag = {"stop": False}
    executor = ThreadPoolExecutor(max_workers=max(1, int(cfg.concurrency)),
                                 thread_name_prefix="ai-gen")

    def _budget_exceeded() -> str | None:
        """派发下一批**之前**检查（在工作池里串行调用，无竞态）。"""
        if time.monotonic() - started > cfg.total_timeout_s:
            return "total_timeout"
        if stats["tokens_used"] >= cfg.token_budget:
            return "token_budget"
        if stats["cost_cny"] >= cfg.cost_budget_cny:
            return "cost_budget"
        if len(accepted) >= cfg.target_count:
            return "target_reached"
        return None

    async def _one_call(index: int) -> None:  # noqa: C901 - 单批调用+校验流程
        async with lock:
            batch_feedback = list(feedback)
        messages = build_prompt(cfg, batch_index=index, feedback=batch_feedback)
        loop = asyncio.get_running_loop()
        try:
            # 🚨 任务卡坑 1：`call_llm_with_failover` 是**同步 def** —— 必须丢到线程里，
            #    直接 await 会阻塞事件循环（那会让「并发 4 路」退化成串行）。
            #    这里用**显式 `ThreadPoolExecutor`** + `run_in_executor`：
            #    语义等同 `asyncio.to_thread`（其内部即 `run_in_executor(None, ...)`
            #    走 asyncio 默认 ThreadPoolExecutor），但好处是
            #    ① 并发上限由本池（4）硬约束，不受默认池大小影响
            #    ② 池的创建/关闭时机可控（见下方 finally）
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    executor, functools.partial(
                        llm_caller, db, messages,
                        profile_id=profile_id,
                        purpose="factor_mining_ai_generate",
                        temperature=cfg.temperature)),
                timeout=cfg.call_timeout_s)
        except asyncio.TimeoutError:
            async with lock:
                stats["calls_failed"] += 1
                stats["calls_made"] += 1
            drops.append({"batch": index, "reason": "call_timeout",
                          "reason_zh": f"单次调用超过 {cfg.call_timeout_s}s"})
            return
        except Exception as exc:  # noqa: BLE001 - 调用器异常也降级
            async with lock:
                stats["calls_failed"] += 1
                stats["calls_made"] += 1
            drops.append({"batch": index, "reason": "call_error",
                          "reason_zh": f"{type(exc).__name__}: {exc}"})
            return

        async with lock:
            stats["calls_made"] += 1
            stats["tokens_used"] += int(getattr(result, "total_tokens", 0) or 0)
            stats["cost_cny"] = round(
                stats["tokens_used"] * COST_PER_TOKEN_CNY, 6)

        if not getattr(result, "success", False):
            async with lock:
                stats["calls_failed"] += 1
            drops.append({
                "batch": index, "reason": "llm_failed",
                "reason_zh": (f"主备均不可用（{getattr(result, 'error_type', None)}: "
                              f"{getattr(result, 'error_message', None)}）")})
            return

        items, parse_error = parse_ai_payload(getattr(result, "raw_response", ""))
        if parse_error:
            async with lock:
                drops.append({"batch": index, "reason": DROP_PARSE_FAILED,
                              "reason_zh": parse_error})
            return

        local_feedback: list[str] = []
        for item in items:
            candidate, drop_reason = validate_skeleton(
                item, cfg=cfg, compiler=compiler)
            if candidate is None:
                async with lock:
                    drops.append({
                        "batch": index, "reason": drop_reason,
                        "reason_zh": DROP_REASON_ZH.get(drop_reason, drop_reason),
                        "formula": str(item.get("formula_ast") or item.get("formula") or "")[:120],
                    })
                if drop_reason == DROP_DUPLICATE or drop_reason == DROP_CONSTRAINT:
                    local_feedback.append(
                        f"{str(item.get('formula_ast') or '')[:60]} → "
                        f"{DROP_REASON_ZH.get(drop_reason, drop_reason)}")
                continue

            digest = _hash_of(str(candidate["formula"]))
            async with lock:
                if digest in accepted_hashes:
                    # 去重反馈重试：记录并在后续批次提示模型换结构
                    drops.append({
                        "batch": index, "reason": DROP_DUPLICATE,
                        "reason_zh": DROP_REASON_ZH[DROP_DUPLICATE],
                        "formula": candidate["formula"][:120],
                        "duplicate_of": accepted_hashes[digest][:120],
                    })
                    local_feedback.append(
                        f"{candidate['formula'][:60]} 已存在，请换结构")
                    continue
                accepted_hashes[digest] = candidate["formula"]
                candidate["formula_hash"] = digest
                accepted.append(candidate)

        if local_feedback:
            async with lock:
                feedback.extend(local_feedback[:MAX_RETRY_PER_SKELETON * 4])

    # ⚠️ 不能用「一次性 fan-out 所有批次」的 gather：
    #    那会让**所有批次的预算/停止预检查都发生在任何调用完成之前**
    #    （check-then-act 竞态）—— token 预算、总超时、目标达成三个停止条件全部失效
    #    （T22 首版实测：4 个停止类测试全红）。
    #    改用**工作池 + 共享游标**：每个 worker 取下一个批次号前先查停止条件，
    #    且工作池大小（=并发路数）天然把执行串行化到检查点之后。
    indices_cursor = {"i": 0}

    async def _worker() -> None:
        while True:
            async with lock:
                if stop_flag["stop"]:
                    return
                if indices_cursor["i"] >= planned:
                    return
                reason = _budget_exceeded()
                if reason:
                    if stats["stopped_reason"] is None:
                        stats["stopped_reason"] = reason
                    stop_flag["stop"] = True
                    return
                index = indices_cursor["i"]
                indices_cursor["i"] += 1
            await _one_call(index)
            async with lock:
                # 每次调用完成后检查目标达成（可观测：即便 planned 已用尽也标记）
                if (stats["stopped_reason"] is None
                        and len(accepted) >= cfg.target_count):
                    stats["stopped_reason"] = "target_reached"
                    stop_flag["stop"] = True

    try:
        await asyncio.gather(*(_worker()
                               for _ in range(max(1, int(cfg.concurrency)))))
    finally:
        executor.shutdown(wait=False)

    stats["accepted"] = len(accepted)
    stats["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    stats["drops_by_reason"] = _count_by_reason(drops)
    shortfall = max(0, cfg.target_count - len(accepted))
    notes: list[str] = []
    if stats["calls_failed"]:
        notes.append(
            f"{stats['calls_failed']}/{stats['calls_made']} 次调用失败/超时，"
            f"对应名额回退受约束随机（需求 §3.3）。")
    if shortfall:
        notes.append(
            f"AI 实际产出 {len(accepted)} 个，目标 {cfg.target_count}，"
            f"缺口 {shortfall} 由**受约束随机补足** —— 属预期行为（需求 §3.3："
            "最坏情况三层损耗后仅剩约 49 < 57）。")
    if stats["stopped_reason"]:
        notes.append(f"提前停止：{stats['stopped_reason']}。")
    return AIResult(candidates=accepted, stats=stats, drops=drops,
                    shortfall=shortfall, notes_zh=notes)


def _count_by_reason(drops: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in drops:
        key = str(d.get("reason") or "unknown")
        out[key] = out.get(key, 0) + 1
    return out


__all__ = [
    "MAX_CALLS",
    "BATCH_MIN",
    "BATCH_MAX",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_CONCURRENCY",
    "CALL_TIMEOUT_S",
    "TOTAL_TIMEOUT_S",
    "TOKEN_BUDGET",
    "COST_BUDGET_CNY",
    "COST_PER_TOKEN_CNY",
    "MAX_RETRY_PER_SKELETON",
    "DEFAULT_TEMPERATURE",
    "CATEGORY_QUOTA_MIN",
    "CATEGORY_QUOTA_MAX",
    "ALL_CATEGORIES",
    "BANNABLE_STRUCTURES",
    "BANNED_STRUCTURE_ZH",
    "FREEDOM_PRESETS",
    "LIMITS",
    "DROP_MISSING_FIELDS",
    "DROP_BAD_CATEGORY",
    "DROP_COMPILE_FAILED",
    "DROP_CONSTRAINT",
    "DROP_DUPLICATE",
    "DROP_PARSE_FAILED",
    "DROP_STRUCTURE_BANNED",
    "DROP_REASON_ZH",
    "FreedomBox",
    "AIGeneratorConfig",
    "AIResult",
    "SYSTEM_PROMPT_HEAD",
    "build_prompt",
    "parse_ai_payload",
    "validate_skeleton",
    "generate_ai_candidates",
]
