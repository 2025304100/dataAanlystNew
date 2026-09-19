"""经典底座初始种群生成（向导 §6.3.3 / §6.3.4；任务 T17）。

流程（严格对应向导 §6.3.4 的 7 步）
=================================
    1. 类别/模板过滤：启用类别 + 字段依赖 ⊆ 已选字段（未选字段的模板跳过，不报错）
    2. 参数网格展开：每个模板按参数定义做笛卡尔积
    3. 批量校验：AST 语法 + 字段依赖 + 复杂度 + 类型（复用 `compile_formula`）
    4. 来源内去重：**T19 负责**（任务卡「明确不做去重本身」）—— 本模块不做
    5. 按覆盖策略分配名额：
       - 均衡覆盖：每类基础名额 = floor(上限 / 启用类别数)，余数按类别优先级补；
         类内按 (模板优先级, 复杂度, 参数序) 升序取 Top；**某类不足的缺口流转给其他类**
       - 优先级覆盖：全部候选统一按 (类别优先级, 模板优先级, 复杂度) 取 Top
    6. 锁定经典底座（实际入库数 ≤ 上限，**不足不硬凑**），operation="enumerated", generation=0
    7. 剩余名额 = 种群大小 − 经典实际入库数（交给 AI/随机填充，T21/T22 消费）

模板数据为什么在代码里而不是库里
==============================
`factor_mining_templates` 表当前**是空的**（实测 0 行），且没有 `category` 列 ——
种库属另一任务。本模块把 25 个系统预设作为**模块常量**承载：
- 系统预设「不可删除」→ 常量天然不可删
- 便于测试与代码评审（公式改动走 diff）

⚠️ `gross_margin` / `asset_turnover` 两条模板**默认禁用**（向导 §6.3.3 注：
数据尚未采集，M2 才启用），它们会被跳过并在 `skipped` 里给出原因。
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.services.factors.mining import category as CAT

logger = logging.getLogger(__name__)

#: 覆盖策略
STRATEGY_BALANCED = "balanced"          # 均衡覆盖（默认）
STRATEGY_PRIORITY = "priority"          # 优先级覆盖
VALID_STRATEGIES: tuple[str, ...] = (STRATEGY_BALANCED, STRATEGY_PRIORITY)

#: 经典底座数量上限（需求 §6.1：默认 40，硬上限 = 种群 60%）
DEFAULT_TEMPLATE_LIMIT = 40
MAX_TEMPLATE_LIMIT = 40                 # 绝对上限（与「默认 40」同值）
POPULATION_SHARE_CAP = 0.6              # 硬上限：不超过种群 60%

#: 单模板参数网格展开上限（防御参数爆炸）
MAX_GRID_PER_TEMPLATE = 64

#: 跳过原因码
SKIP_CATEGORY_DISABLED = "category_disabled"
SKIP_FIELD_MISSING = "field_missing"
SKIP_TEMPLATE_DISABLED = "template_disabled"
SKIP_COMPILE_FAILED = "compile_failed"
SKIP_LIMIT_REACHED = "limit_reached"

SKIP_REASON_ZH: dict[str, str] = {
    SKIP_CATEGORY_DISABLED: "该类别未启用",
    SKIP_FIELD_MISSING: "依赖字段未在已选字段内",
    SKIP_TEMPLATE_DISABLED: "模板已禁用（数据未接入）",
    SKIP_COMPILE_FAILED: "公式未通过编译校验",
    SKIP_LIMIT_REACHED: "受数量上限限制未入选",
}


# ══════════════════════════════════════════════════════════
# 25 个系统预设模板（向导 §6.3.3，公式已按 v3.2 方言归一）
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ClassicTemplate:
    """经典模板定义。

    `params` 是**有序**参数名 → 候选值列表；展开时做笛卡尔积并保序
    （保序让「同模板同参数的候选顺序稳定」，便于增量复现）。
    """

    name: str
    category: str
    formula: str
    params: Mapping[str, Sequence[int | float]]
    required_fields: tuple[str, ...]
    economy_logic_zh: str
    priority: int = 5               # 数字越小越优先（同类别内排序用）
    enabled: bool = True

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(self.params.keys())

    def grid_size(self) -> int:
        n = 1
        for values in self.params.values():
            n *= max(1, len(values))
        return n


CLASSIC_TEMPLATES: tuple[ClassicTemplate, ...] = (
    # ── 趋势类（5）──
    ClassicTemplate(
        name="均线趋势", category=CAT.CATEGORY_TREND,
        formula="mean(close,{n1})/mean(close,{n2})-1",
        params={"n1": (5, 10, 20), "n2": (20, 60, 120)},
        required_fields=("close",), priority=1,
        economy_logic_zh="短期均线上穿长期均线，捕捉趋势启动",
    ),
    ClassicTemplate(
        name="价格动量", category=CAT.CATEGORY_TREND,
        formula="ts_delta_bars(close,{n1})",
        params={"n1": (10, 20, 60)},
        required_fields=("close",), priority=2,
        economy_logic_zh="过去 N 日涨跌幅，捕捉价格动量",
    ),
    ClassicTemplate(
        name="通道突破", category=CAT.CATEGORY_TREND,
        formula="(close-lowest(low,{n1}))/(highest(high,{n1})-lowest(low,{n1}))",
        params={"n1": (20, 60)},
        required_fields=("close", "low", "high"), priority=3,
        economy_logic_zh="价格在 N 日通道中的位置，突破上沿看多",
    ),
    ClassicTemplate(
        name="MACD", category=CAT.CATEGORY_TREND,
        formula="ema(close,{n1})-ema(close,{n2})",
        params={"n1": (12,), "n2": (26,)},
        required_fields=("close",), priority=4,
        economy_logic_zh="快慢均线差值，趋势强度指标",
    ),
    ClassicTemplate(
        name="均线斜率", category=CAT.CATEGORY_TREND,
        formula="(mean(close,{n1})-mean(close,{n2}))/{n2}",
        params={"n1": (5, 10), "n2": (20, 60)},
        required_fields=("close",), priority=5,
        economy_logic_zh="短期均线相对长期均线的斜率，趋势加速度",
    ),
    # ── 反转类（4）──
    ClassicTemplate(
        name="短期反转", category=CAT.CATEGORY_REVERSAL,
        formula="-ts_delta_bars(close,{n1})",
        params={"n1": (5, 10)},
        required_fields=("close",), priority=1,
        economy_logic_zh="过去 N 日跌幅越大，未来反弹概率越高",
    ),
    ClassicTemplate(
        name="超买超卖", category=CAT.CATEGORY_REVERSAL,
        formula="(close-lowest(low,{n1}))/(highest(high,{n1})-lowest(low,{n1}))-0.5",
        params={"n1": (14, 20)},
        required_fields=("close", "low", "high"), priority=2,
        economy_logic_zh="类似 RSI，超卖（<0）看多、超买（>0）看空",
    ),
    ClassicTemplate(
        name="均值回归", category=CAT.CATEGORY_REVERSAL,
        formula="-(close/mean(close,{n1})-1)",
        params={"n1": (20, 60)},
        required_fields=("close",), priority=3,
        economy_logic_zh="价格偏离均线越远，回归概率越高",
    ),
    ClassicTemplate(
        name="量价背离", category=CAT.CATEGORY_REVERSAL,
        formula="-ts_delta_bars(close,{n1}) * cs_rank(volume)",
        params={"n1": (5, 10)},
        required_fields=("close", "volume"), priority=4,
        economy_logic_zh="价格下跌但成交量放大，可能是底部信号",
    ),
    # ── 波动率类（4）──
    ClassicTemplate(
        name="波动率收缩", category=CAT.CATEGORY_VOLATILITY,
        formula="stddev(close,{n1})/mean(stddev(close,{n2}),{n3})",
        params={"n1": (5, 10), "n2": (20, 60), "n3": (20, 60)},
        required_fields=("close",), priority=1,
        economy_logic_zh="当前波动率相对历史波动率的位置，收缩后可能突破",
    ),
    ClassicTemplate(
        name="波动率突破", category=CAT.CATEGORY_VOLATILITY,
        formula="ts_delta_bars(stddev(close,{n1}),{n2})",
        params={"n1": (5, 10), "n2": (20, 60)},
        required_fields=("close",), priority=2,
        economy_logic_zh="波动率突然放大，可能是趋势启动信号",
    ),
    ClassicTemplate(
        name="ATR比率", category=CAT.CATEGORY_VOLATILITY,
        formula="ts_atr(close,{n1})/close",
        params={"n1": (14, 20)},
        required_fields=("close",), priority=3,
        economy_logic_zh="真实波幅占价格比例，波动率水平",
    ),
    ClassicTemplate(
        name="布林带位置", category=CAT.CATEGORY_VOLATILITY,
        formula="(close-mean(close,{n1}))/(2*stddev(close,{n1}))",
        params={"n1": (20,)},
        required_fields=("close",), priority=4,
        economy_logic_zh="价格在布林带中的位置，±1 为上下轨",
    ),
    # ── 估值类（4）──
    ClassicTemplate(
        name="PE反转", category=CAT.CATEGORY_VALUATION,
        formula="-cs_rank(pe_ttm)",
        params={}, required_fields=("pe_ttm",), priority=1,
        economy_logic_zh="低 PE 股票未来收益可能更高（价值效应）",
    ),
    ClassicTemplate(
        name="PB反转", category=CAT.CATEGORY_VALUATION,
        formula="-cs_rank(pb)",
        params={}, required_fields=("pb",), priority=2,
        economy_logic_zh="低 PB 股票未来收益可能更高（价值效应）",
    ),
    ClassicTemplate(
        name="估值比价", category=CAT.CATEGORY_VALUATION,
        formula="cs_rank(pe_ttm)-cs_rank(pb)",
        params={}, required_fields=("pe_ttm", "pb"), priority=3,
        economy_logic_zh="PE 和 PB 的相对位置，寻找估值错配",
    ),
    ClassicTemplate(
        name="盈利收益率", category=CAT.CATEGORY_VALUATION,
        formula="cs_rank(1/pe_ttm)",
        params={}, required_fields=("pe_ttm",), priority=4,
        economy_logic_zh="盈利收益率（E/P），价值因子的另一种表达",
    ),
    # ── 质量类（4，其中 2 条禁用）──
    ClassicTemplate(
        name="ROE变化", category=CAT.CATEGORY_QUALITY,
        formula="ts_delta_periods(roe_ttm,{n1})",
        params={"n1": (1, 2, 4)},
        required_fields=("roe_ttm",), priority=1,
        economy_logic_zh="ROE 提升的公司未来收益可能更好（盈利动量）",
    ),
    ClassicTemplate(
        name="盈利加速度", category=CAT.CATEGORY_QUALITY,
        formula="ts_delta_periods(roe_ttm,{n1})-ts_delta_periods(roe_ttm,{n2})",
        params={"n1": (1,), "n2": (2, 4)},
        required_fields=("roe_ttm",), priority=2,
        economy_logic_zh="ROE 变化的变化，盈利加速的公司更优",
    ),
    ClassicTemplate(
        name="毛利率变化", category=CAT.CATEGORY_QUALITY,
        formula="ts_delta_periods(gross_margin,{n1})",
        params={"n1": (1, 2, 4)},
        required_fields=("gross_margin",), priority=3,
        economy_logic_zh="毛利率提升说明竞争力增强",
        enabled=False,   # 向导 §6.3.3 注：数据未采集（M2）
    ),
    ClassicTemplate(
        name="资产周转率", category=CAT.CATEGORY_QUALITY,
        formula="ts_delta_periods(asset_turnover,{n1})",
        params={"n1": (1, 2, 4)},
        required_fields=("asset_turnover",), priority=4,
        economy_logic_zh="资产周转率提升说明运营效率改善",
        enabled=False,   # 同上
    ),
    # ── 量价类（4）──
    ClassicTemplate(
        name="换手率异常", category=CAT.CATEGORY_VOLUME_PRICE,
        formula="cs_rank(turnover_rate)-cs_rank(mean(turnover_rate,{n1}))",
        params={"n1": (20, 60)},
        required_fields=("turnover_rate",), priority=1,
        economy_logic_zh="当前换手率相对历史均值的偏离，异常放量可能有行情",
    ),
    ClassicTemplate(
        name="成交量趋势", category=CAT.CATEGORY_VOLUME_PRICE,
        formula="mean(volume,{n1})/mean(volume,{n2})-1",
        params={"n1": (5, 10), "n2": (20, 60)},
        required_fields=("volume",), priority=2,
        economy_logic_zh="短期成交量相对长期的变化，放量看涨",
    ),
    ClassicTemplate(
        name="量价背离(量价类)", category=CAT.CATEGORY_VOLUME_PRICE,
        formula="cs_rank(volume) - cs_rank(ts_delta_bars(close,{n1}))",
        params={"n1": (5, 10)},
        required_fields=("volume", "close"), priority=3,
        economy_logic_zh="成交量大但价格不涨，可能是出货信号",
    ),
    ClassicTemplate(
        name="资金流强度", category=CAT.CATEGORY_VOLUME_PRICE,
        formula="amount/mean(amount,{n1})",
        params={"n1": (20, 60)},
        required_fields=("amount",), priority=4,
        economy_logic_zh="当前成交额相对于历史均值的强度",
    ),
)

TEMPLATE_COUNT = len(CLASSIC_TEMPLATES)


# ══════════════════════════════════════════════════════════
# 展开 / 校验
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ExpandedCandidate:
    """一个具体候选公式（参数已代入）。"""

    template_name: str
    category: str
    formula: str
    params: dict[str, Any]
    required_fields: tuple[str, ...]
    economy_logic_zh: str
    priority: int
    param_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_name": self.template_name,
            "category": self.category,
            "formula": self.formula,
            "params": dict(self.params),
            "required_fields": list(self.required_fields),
            "economy_logic_zh": self.economy_logic_zh,
            "priority": self.priority,
            "param_index": self.param_index,
            "operation": "enumerated",
            "generation": 0,
        }


@dataclass
class SkippedItem:
    reason: str
    reason_zh: str
    detail: str
    template_name: str | None = None
    category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "reason_zh": self.reason_zh,
            "detail": self.detail,
            "template_name": self.template_name,
            "category": self.category,
        }


@dataclass
class InitialPopulationResult:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    strategy: str = STRATEGY_BALANCED
    template_limit: int = DEFAULT_TEMPLATE_LIMIT
    population_size: int = 0
    remaining_slots: int = 0
    per_category: dict[str, int] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    compile_errors: list[dict[str, Any]] = field(default_factory=list)
    notes_zh: list[str] = field(default_factory=list)
    #: 经典底座「不足不硬凑」时为 True
    insufficient: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "strategy": self.strategy,
            "template_limit": self.template_limit,
            "classic_count": len(self.candidates),
            "population_size": self.population_size,
            "remaining_slots": self.remaining_slots,
            "per_category": dict(self.per_category),
            "skipped": self.skipped,
            "compile_errors": self.compile_errors,
            "insufficient": self.insufficient,
            "notes_zh": self.notes_zh,
        }


def expand_template(template: ClassicTemplate) -> list[ExpandedCandidate]:
    """参数网格展开（笛卡尔积，保序）。超过 `MAX_GRID_PER_TEMPLATE` 时截断。"""
    names = template.param_names
    if not names:
        return [ExpandedCandidate(
            template_name=template.name, category=template.category,
            formula=template.formula, params={},
            required_fields=template.required_fields,
            economy_logic_zh=template.economy_logic_zh,
            priority=template.priority, param_index=0)]

    value_lists = [list(template.params[n]) for n in names]
    combos = list(itertools.product(*value_lists))
    if len(combos) > MAX_GRID_PER_TEMPLATE:
        combos = combos[:MAX_GRID_PER_TEMPLATE]

    out: list[ExpandedCandidate] = []
    for idx, combo in enumerate(combos):
        params = dict(zip(names, combo))
        out.append(ExpandedCandidate(
            template_name=template.name, category=template.category,
            formula=template.formula.format(**params), params=params,
            required_fields=template.required_fields,
            economy_logic_zh=template.economy_logic_zh,
            priority=template.priority, param_index=idx))
    return out


def _is_negated(formula: str) -> bool:
    """粗略判定表达式是否整体取负（反转特征）。

    只看「以 `-` 开头」或「`-(...)`」两类 —— 保守判定：
    漏判只会让归类落到 trend（兜底类），不会误判成 reversal。
    """
    stripped = formula.strip()
    return stripped.startswith("-") or stripped.startswith("0-")


def build_candidates(
    *, selected_fields: Iterable[str] | None = None,
    enabled_categories: Iterable[str] | None = None,
    templates: Sequence[ClassicTemplate] | None = None,
) -> tuple[list[dict[str, Any]], list[SkippedItem]]:
    """步骤 1~2：过滤 + 展开（**不编译**）。

    返回 `(candidates, skipped)`；每个 candidate 带 `category`（按优先级归类）
    与 `fields`（依赖字段），供后续分配名额。
    """
    fields = set(selected_fields) if selected_fields is not None else None
    cats = set(enabled_categories) if enabled_categories is not None \
        else set(CAT.ALL_CATEGORIES)

    out: list[dict[str, Any]] = []
    skipped: list[SkippedItem] = []
    for tpl in (templates or CLASSIC_TEMPLATES):
        if not tpl.enabled:
            skipped.append(SkippedItem(
                reason=SKIP_TEMPLATE_DISABLED,
                reason_zh=SKIP_REASON_ZH[SKIP_TEMPLATE_DISABLED],
                detail=f"模板「{tpl.name}」因数据未接入被禁用（向导 §6.3.3 注）。",
                template_name=tpl.name, category=tpl.category))
            continue
        if tpl.category not in cats:
            skipped.append(SkippedItem(
                reason=SKIP_CATEGORY_DISABLED,
                reason_zh=SKIP_REASON_ZH[SKIP_CATEGORY_DISABLED],
                detail=f"类别「{CAT.CATEGORY_LABELS_ZH[tpl.category]}」未启用。",
                template_name=tpl.name, category=tpl.category))
            continue
        if fields is not None:
            missing = [f for f in tpl.required_fields if f not in fields]
            if missing:
                skipped.append(SkippedItem(
                    reason=SKIP_FIELD_MISSING,
                    reason_zh=SKIP_REASON_ZH[SKIP_FIELD_MISSING],
                    detail=f"模板「{tpl.name}」依赖字段 {missing} 未选中，跳过（不报错）。",
                    template_name=tpl.name, category=tpl.category))
                continue

        for cand in expand_template(tpl):
            row = cand.to_dict()
            cls = CAT.classify(
                fields=cand.required_fields,
                functions=_functions_of(cand.formula),
                negated=_is_negated(cand.formula),
                declared=cand.category,
            )
            row["category"] = cls.category
            row["category_declared"] = cand.category
            row["category_reason_zh"] = cls.reason_zh
            row["category_all_matches"] = list(cls.all_matches)
            row["negated"] = _is_negated(cand.formula)
            out.append(row)
    return out, skipped


def _functions_of(formula: str) -> list[str]:
    """从公式文本抽取算子名（`name(` 形式）—— 供归类用。"""
    import re

    return re.findall(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(", formula or "")


def validate_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *, compiler: Callable[..., Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """步骤 3：批量编译校验。返回 `(通过列表, 编译错误列表)`。

    通过的候选补充 `complexity` / `node_count` / `max_lookback` / `direction`，
    并标注 `compile_ok=True`。**失败的候选不进种群**（向导 §6.3.4 步骤 3）。

    Args:
        compiler: 可注入的编译函数 `(formula, params) -> CompilationResult`；
            缺省用 `factor_compiler.compile_formula`（测试用假编译器避免真编译开销）。
    """
    if compiler is None:
        from app.services.factors.factor_compiler import compile_formula

        compiler = lambda *, formula, params=None: compile_formula(  # noqa: E731
            formula=formula, params=params)

    ok: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for cand in candidates:
        result = compiler(formula=str(cand["formula"]),
                          params=dict(cand.get("params") or {}))
        if result.success and result.execution_plan is not None:
            plan = result.execution_plan
            row = dict(cand)
            row.update({
                "compile_ok": True,
                "canonical_formula": plan.formula,
                "complexity": float(plan.complexity_score),
                "node_count": int(plan.node_count),
                "ast_depth": int(plan.ast_depth),
                "max_lookback": int(plan.max_lookback),
                "direction": plan.direction,
                "compiler_version": plan.compiler_version,
            })
            ok.append(row)
        else:
            errors.append({
                "formula": cand["formula"],
                "template_name": cand.get("template_name"),
                "category": cand.get("category"),
                "errors": [
                    {"code": getattr(e, "error_code", None),
                     "message": getattr(e, "message", str(e))}
                    for e in (result.errors or [])
                ],
            })
    return ok, errors


# ══════════════════════════════════════════════════════════
# 步骤 5：名额分配
# ══════════════════════════════════════════════════════════


def _sort_within_category(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """类内排序：模板优先级 → 复杂度 → 参数序（稳定且可复现）。"""
    return sorted(rows, key=lambda r: (
        int(r.get("priority", 99)),
        float(r.get("complexity", 0.0)),
        int(r.get("param_index", 0)),
    ))


def allocate_balanced(
    by_category: Mapping[str, Sequence[dict[str, Any]]], *, limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """均衡覆盖：每类基础名额 = floor(limit / 类别数)，余数与**缺口**按优先级流转。

    返回 `(入选列表, 因上限未入选列表)`。

    缺口流转（任务卡硬规则）：
    - 某类候选不足基础名额 → 其剩余名额释放给**其他类**（按类别优先级依次取）
    - 余数（limit % 类别数）先按类别优先级各补 1
    """
    present = [c for c in CAT.CATEGORY_PRIORITY if by_category.get(c)]
    if not present:
        return [], []

    limit = max(0, int(limit))
    base = limit // len(present)
    remainder = limit % len(present)

    picked: list[dict[str, Any]] = []
    leftovers: dict[str, list[dict[str, Any]]] = {}
    pools: dict[str, list[dict[str, Any]]] = {
        c: _sort_within_category(list(by_category[c])) for c in present
    }

    # ① 基础名额
    for c in present:
        take = base
        chosen = pools[c][:take]
        picked.extend(chosen)
        leftovers[c] = pools[c][take:]

    # ② 余数：按类别优先级各补 1（补到没有候选为止）
    for c in present:
        if remainder <= 0:
            break
        if leftovers[c]:
            picked.append(leftovers[c].pop(0))
            remainder -= 1
    # 余数还没发完（某些类已空）→ 继续按优先级补
    while remainder > 0:
        progressed = False
        for c in present:
            if remainder <= 0:
                break
            if leftovers[c]:
                picked.append(leftovers[c].pop(0))
                remainder -= 1
                progressed = True
        if not progressed:
            break

    # ③ 缺口流转：把全部剩余名额按类别优先级依次发完（直到 limit 或无候选）
    while len(picked) < limit:
        progressed = False
        for c in present:
            if len(picked) >= limit:
                break
            if leftovers[c]:
                picked.append(leftovers[c].pop(0))
                progressed = True
        if not progressed:
            break

    unselected: list[dict[str, Any]] = []
    for c in present:
        unselected.extend(leftovers[c])
    return picked, unselected


def allocate_priority(
    candidates: Sequence[dict[str, Any]], *, limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """优先级覆盖：全体统一按 (类别优先级, 模板优先级, 复杂度, 参数序) 取 Top。"""
    ordered = sorted(candidates, key=lambda r: (
        CAT.priority_of(str(r.get("category"))),
        int(r.get("priority", 99)),
        float(r.get("complexity", 0.0)),
        int(r.get("param_index", 0)),
    ))
    limit = max(0, int(limit))
    return ordered[:limit], ordered[limit:]


# ══════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════


def resolve_template_limit(*, population_size: int,
                           requested: int | None = None) -> tuple[int, list[str]]:
    """解析经典底座上限（需求 §6.1：默认 40；硬上限 = 种群 60%）。

    Returns:
        `(上限, 提示列表)`；被硬上限压过时给出提示（**不静默**）。
    """
    notes: list[str] = []
    limit = DEFAULT_TEMPLATE_LIMIT if requested is None else int(requested)
    if limit < 0:
        raise ValueError("template_limit 不能为负。")
    if limit > MAX_TEMPLATE_LIMIT:
        notes.append(f"请求上限 {limit} 超过绝对上限 {MAX_TEMPLATE_LIMIT}，已钳制。")
        limit = MAX_TEMPLATE_LIMIT
    hard_cap = int(max(0, population_size) * POPULATION_SHARE_CAP)
    if population_size > 0 and limit > hard_cap:
        notes.append(
            f"经典底座上限受「不超过种群 {POPULATION_SHARE_CAP:.0%}」约束，"
            f"由 {limit} 降为 {hard_cap}。")
        limit = hard_cap
    return limit, notes


def build_initial_population(
    *, population_size: int,
    selected_fields: Iterable[str] | None = None,
    enabled_categories: Iterable[str] | None = None,
    template_limit: int | None = None,
    strategy: str = STRATEGY_BALANCED,
    templates: Sequence[ClassicTemplate] | None = None,
    compiler: Any | None = None,
) -> InitialPopulationResult:
    """经典底座生成主入口（向导 §6.3.4 步骤 1~7）。

    ⚠️ 步骤 4「来源内去重」由 **T19** 负责，本模块**不做**去重
    （任务卡「明确不做去重本身」）。
    """
    if strategy not in VALID_STRATEGIES:
        raise ValueError(f"strategy 必须是 {list(VALID_STRATEGIES)} 之一。")

    limit, notes = resolve_template_limit(population_size=population_size,
                                          requested=template_limit)

    raw, skipped = build_candidates(
        selected_fields=selected_fields, enabled_categories=enabled_categories,
        templates=templates)
    if notes:
        logger.info("初始种群：%s", "；".join(notes))

    # 类别过滤要把「归类后落到未启用类别」的候选也剔掉
    cats = set(enabled_categories) if enabled_categories is not None \
        else set(CAT.ALL_CATEGORIES)
    kept: list[dict[str, Any]] = []
    for row in raw:
        if row["category"] in cats:
            kept.append(row)
        else:
            skipped.append(SkippedItem(
                reason=SKIP_CATEGORY_DISABLED,
                reason_zh=SKIP_REASON_ZH[SKIP_CATEGORY_DISABLED],
                detail=(f"候选 {row['formula']} 归类为「"
                        f"{CAT.CATEGORY_LABELS_ZH.get(row['category'], row['category'])}」"
                        "，该类别未启用。"),
                template_name=row.get("template_name"),
                category=row.get("category")))

    compiled, compile_errors = validate_candidates(kept, compiler=compiler)
    for err in compile_errors:
        skipped.append(SkippedItem(
            reason=SKIP_COMPILE_FAILED,
            reason_zh=SKIP_REASON_ZH[SKIP_COMPILE_FAILED],
            detail=f"公式 {err['formula']} 编译失败：{err['errors'][:1]}",
            template_name=err.get("template_name"), category=err.get("category")))

    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in compiled:
        by_category.setdefault(str(row["category"]), []).append(row)

    if strategy == STRATEGY_BALANCED:
        picked, unselected = allocate_balanced(by_category, limit=limit)
    else:
        picked, unselected = allocate_priority(compiled, limit=limit)

    if unselected:
        per_cat_limit: dict[str, int] = {}
        for row in unselected:
            c = str(row.get("category"))
            per_cat_limit[c] = per_cat_limit.get(c, 0) + 1
        for c, n in per_cat_limit.items():
            skipped.append(SkippedItem(
                reason=SKIP_LIMIT_REACHED,
                reason_zh=SKIP_REASON_ZH[SKIP_LIMIT_REACHED],
                detail=f"类别「{CAT.CATEGORY_LABELS_ZH.get(c, c)}」有 {n} 个候选"
                       f"因数量上限 {limit} 未入选（**不硬凑、也不丢弃选择权**）。",
                category=c))

    per_category = {c: 0 for c in CAT.CATEGORY_PRIORITY}
    for row in picked:
        per_category[row["category"]] = per_category.get(row["category"], 0) + 1

    classic_count = len(picked)
    remaining = max(0, int(population_size) - classic_count)
    insufficient = classic_count < limit
    if insufficient:
        notes.append(
            f"经典底座实际 {classic_count} 个，低于上限 {limit} —— "
            "**不足不硬凑**，缺口由 AI/随机填充（向导 §6.3.4 步骤 6/7）。")

    return InitialPopulationResult(
        candidates=picked, strategy=strategy, template_limit=limit,
        population_size=int(population_size), remaining_slots=remaining,
        per_category={k: v for k, v in per_category.items() if v},
        skipped=[s.to_dict() for s in skipped],
        compile_errors=compile_errors,
        notes_zh=notes, insufficient=insufficient,
    )


__all__ = [
    "STRATEGY_BALANCED",
    "STRATEGY_PRIORITY",
    "VALID_STRATEGIES",
    "DEFAULT_TEMPLATE_LIMIT",
    "MAX_TEMPLATE_LIMIT",
    "POPULATION_SHARE_CAP",
    "MAX_GRID_PER_TEMPLATE",
    "SKIP_CATEGORY_DISABLED",
    "SKIP_FIELD_MISSING",
    "SKIP_TEMPLATE_DISABLED",
    "SKIP_COMPILE_FAILED",
    "SKIP_LIMIT_REACHED",
    "SKIP_REASON_ZH",
    "ClassicTemplate",
    "CLASSIC_TEMPLATES",
    "TEMPLATE_COUNT",
    "ExpandedCandidate",
    "SkippedItem",
    "InitialPopulationResult",
    "expand_template",
    "build_candidates",
    "validate_candidates",
    "allocate_balanced",
    "allocate_priority",
    "resolve_template_limit",
    "build_initial_population",
]
