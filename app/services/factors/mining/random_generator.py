"""受约束随机生成器（向导 §6.3.6；任务 T18）。

**不是自由随机**（任务卡「明确不做：不做自由随机，必须受约束」）：
随机是在「类型签名 + 复杂度 + 哈希拦截 + 结构限变体」四重约束下的组合搜索。

约束（向导 §6.3.6 第 1~8 条）
==========================
1. **字段分类型**：价格 / 成交量 / 换手率 / 估值 / 质量 / 资金流
2. **算子带类型签名**：`mean(价格类)→价格类`、`cs_rank(任意)→无量纲`、`+` 同类型
3. **类型匹配**：同类型才能加减；截面算子输出无量纲可跨类型
4. **复杂度上限**：算子 ≤ 4、嵌套 ≤ 2、字段引用 ≤ 3
5. 必须通过 AST 校验（复用 `factor_compiler.compile_formula`）
7. **实时哈希拦截**：生成即与所有已有（经典+AI+随机）比对，撞了立刻重生成
   （单公式最多 20 次尝试）
8. **同结构限变体**：相同 AST 结构（仅参数不同）每种最多 3 个
9. 循环到 `目标数量 × 冗余系数 2.0`
10/11/12. 批量校验 + 去重 + **多样性排序**（与已有底座/候选相似度低者优先）取 Top N

生成流程（第 9~12 条）
====================
`generate_random_candidates()` 一次调用走完：生成 → 拦截 → 校验 → 排序 → 取 Top。
`backfill_count` 用于「AI 生成不足时补位」（需求 §6.1 的数量链路）。

为什么不用 `log` / `exp`
======================
编译器的 26 个算子里有 `log`/`exp`，但它们在价格/成交额这类量纲上会极端放大离群值，
属「随机生成必须保证基础有效性」的例外。本生成器**只使用** math 类的 `abs`/`min`/`max`
/`round`/`sqrt`，并把排除原因写在 `EXCLUDED_FUNCTIONS` 里（**不是遗漏，是裁决**）。
"""
from __future__ import annotations

import random
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.services.factors.mining import config_hash as CH

# ══════════════════════════════════════════════════════════
# 约束常量（向导 §6.3.6）
# ══════════════════════════════════════════════════════════

#: 复杂度上限：算子数 / 嵌套深度 / 字段引用数
MAX_OPERATORS = 4
MAX_NESTING = 2
MAX_FIELD_REFS = 3

#: 同 AST 结构最多保留的参数变体数
MAX_VARIANTS_PER_STRUCTURE = 3

#: 单公式哈希碰撞后的最大重试次数
MAX_ATTEMPTS_PER_FORMULA = 20

#: 冗余系数（向导 §6.3.7：目标数量 × 2.0）
REDUNDANCY_FACTOR = 2.0

#: 窗口参数范围
WINDOW_MIN = 5
WINDOW_MAX = 120

#: 随机窗口取值池（避免连续空间产生的密集近似重复）
WINDOW_POOL: tuple[int, ...] = (5, 10, 14, 20, 30, 60, 90, 120)

#: 候选来源标记
SOURCE_RANDOM = "random"
OPERATION_RANDOM = "random"

# ══════════════════════════════════════════════════════════
# 类型系统
# ══════════════════════════════════════════════════════════

T_PRICE = "price"
T_VOLUME = "volume"
T_TURNOVER = "turnover"
T_VALUATION = "valuation"
T_QUALITY = "quality"
T_FLOW = "flow"
T_DIMENSIONLESS = "dimensionless"

ALL_TYPES: tuple[str, ...] = (T_PRICE, T_VOLUME, T_TURNOVER, T_VALUATION,
                              T_QUALITY, T_FLOW)

#: 字段 → 类型（向导 §6.3.6 第 1 条；覆盖 `FIELD_CATALOG` 的 21 个字段）
FIELD_TYPES: dict[str, str] = {
    "close": T_PRICE, "open": T_PRICE, "high": T_PRICE, "low": T_PRICE,
    "prev_close": T_PRICE,
    "volume": T_VOLUME, "amount": T_VOLUME,
    "turnover_rate": T_TURNOVER,
    "pe_ttm": T_VALUATION, "pb": T_VALUATION,
    "dividend_yield": T_VALUATION, "total_market_cap": T_VALUATION,
    "circulating_market_cap": T_VALUATION,
    "roe_ttm": T_QUALITY,
    "main_net_inflow": T_FLOW,
    "lhb_institution_net": T_FLOW, "hot_rank_pct": T_FLOW,
    "proxy_score": T_FLOW,
    "etf_premium_discount": T_VALUATION, "etf_tracking_error": T_VALUATION,
    "etf_fund_size": T_VALUATION,
}


@dataclass(frozen=True)
class OpSignature:
    """算子的类型签名：输入类型 → 输出类型。

    `inputs`：
    - 具体类型串（`"price"`）→ 该槽位必须是此类型
    - `"same"` → 与第 0 个参数同类型
    - `"any"` → 任意类型
    - `"window"` → 整型窗口参数（不是数据节点）
    """

    name: str
    inputs: tuple[str, ...]
    output: str            # 具体类型 | "same" | "dimensionless"
    category: str
    generator: bool = True  # 是否参与随机生成

    def resolve_output(self, arg_types: Sequence[str]) -> str:
        if self.output == "same":
            for t in arg_types:
                if t != "window" and t != T_DIMENSIONLESS:
                    return t
            return arg_types[0] if arg_types else T_DIMENSIONLESS
        return self.output


#: 参与随机生成的算子签名（**只包含编译器 `FUNCTION_CATALOG` 里真实存在的名字**）
OP_SIGNATURES: dict[str, OpSignature] = {
    # ── 滚动（序列, 窗口）→ 同类型 ──
    "mean": OpSignature("mean", ("any", "window"), "same", "rolling"),
    "sma": OpSignature("sma", ("any", "window"), "same", "rolling"),
    "ema": OpSignature("ema", ("any", "window"), "same", "rolling"),
    "sum": OpSignature("sum", ("any", "window"), "same", "rolling"),
    "stddev": OpSignature("stddev", ("any", "window"), T_DIMENSIONLESS, "rolling"),
    "highest": OpSignature("highest", ("any", "window"), "same", "rolling"),
    "lowest": OpSignature("lowest", ("any", "window"), "same", "rolling"),
    "ref": OpSignature("ref", ("any", "window"), "same", "rolling"),
    "count": OpSignature("count", ("any", "window"), T_DIMENSIONLESS, "rolling"),
    "pct_change": OpSignature("pct_change", ("any", "window"), T_DIMENSIONLESS,
                              "rolling"),
    # ── 时序 ──
    "ts_delta_bars": OpSignature("ts_delta_bars", ("any", "window"), "same",
                                 "timeseries"),
    "ts_delta_periods": OpSignature("ts_delta_periods", ("any", "window"), "same",
                                    "timeseries"),
    "ts_atr": OpSignature("ts_atr", ("any", "window"), "same", "timeseries"),
    # ── 截面 ──
    # 统一规则（T18 复核后定，见下方「归一化 vs 保形」注释）：
    #   归一化类（丢单位丢域）→ dimensionless：cs_rank / cs_zscore / cs_scale / cs_quantile
    #   保形类（保单位保域）→ same：cs_demean / cs_winsorize
    "cs_rank": OpSignature("cs_rank", ("any",), T_DIMENSIONLESS, "cross_section"),
    "cs_zscore": OpSignature("cs_zscore", ("any",), T_DIMENSIONLESS, "cross_section"),
    "cs_demean": OpSignature("cs_demean", ("any",), "same", "cross_section"),
    "cs_scale": OpSignature("cs_scale", ("any",), T_DIMENSIONLESS, "cross_section"),
    "cs_quantile": OpSignature("cs_quantile", ("any", "window"), T_DIMENSIONLESS,
                               "cross_section"),
    "cs_winsorize": OpSignature("cs_winsorize", ("any", "window"), "same",
                                "cross_section"),
    # ── math（刻意排除 log / exp，见模块 docstring）──
    "abs": OpSignature("abs", ("any",), "same", "math"),
    # sqrt：严格量纲分析会改单位（¥^0.5 ↔ ¥），这里按「同域单调变换」处理 ——
    # 属**工程惯例**而非严格量纲，写成注释避免后来者以为算错了。
    "sqrt": OpSignature("sqrt", ("any",), "same", "math"),
    "min": OpSignature("min", ("same", "same"), "same", "math"),
    "max": OpSignature("max", ("same", "same"), "same", "math"),
    "round": OpSignature("round", ("any", "window"), "same", "math"),
}

#: 量纲判别的**可推导原则**（比逐条记忆可靠）：
#:
#: - **归一化类**（把原值除以/换算成与自身尺度相关的量）→ 单位相消 → 无量纲、且**丢域**：
#:   `cs_rank`（百分位）、`cs_zscore`（(x-μ)/σ）、`cs_scale`（x/Σ|x|）、
#:   `cs_quantile`（组号）、`stddev`（与 x 同单位但作为「离散度」不再属于价格域）、
#:   `pct_change`（比值）、`count`（计数）
#: - **保形类**（只做平移/截尾/滚动聚合同一量）→ 保单位、保域：
#:   `cs_demean`（x-μ）、`cs_winsorize`（截尾）、`mean`/`sma`/`ema`/`sum`/
#:   `highest`/`lowest`/`ref`、`ts_delta_bars`/`ts_delta_periods`/`ts_atr`、
#:   `abs`/`min`/`max`/`round`
#:
#: ⚠️ 曾经的错判：把 `cs_scale` 归入保形类。它的实现是 `values / Σ|x|`
#: （`dsl/cross_section.py::cs_scale`），单位相消 → 实为无量纲。
#: 判据不要看名字前缀（`cs_` 不等于「一律无量纲」），要看**是否除以了同量纲的量**。

#: **刻意排除**的算子（在编译器里存在，但随机生成不用）—— 不是遗漏
EXCLUDED_FUNCTIONS: dict[str, str] = {
    "log": "对价格/成交额等量纲会极端放大离群值，随机生成需保证基础有效性",
    "exp": "同上，且易溢出",
}

#: 二元运算符（`+`/`-` 要求同类型；`*`/`/` 至少一侧无量纲）
BINARY_OPS: tuple[str, ...] = ("+", "-", "*", "/")


def field_type(field_name: str) -> str | None:
    return FIELD_TYPES.get(str(field_name))


def available_fields_by_type(selected_fields: Iterable[str] | None) -> dict[str, list[str]]:
    """按类型分组可选字段（只保留已选且类型已知的）。"""
    out: dict[str, list[str]] = {}
    for f in (selected_fields if selected_fields is not None else FIELD_TYPES.keys()):
        t = field_type(f)
        if t:
            out.setdefault(t, []).append(str(f))
    for v in out.values():
        v.sort()
    return out


# ══════════════════════════════════════════════════════════
# 复杂度度量 / 结构键
# ══════════════════════════════════════════════════════════

_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(")
_FIELD_RE = re.compile(r"(?<![A-Za-z_0-9])([a-z_][a-z_0-9]*)(?![A-Za-z_0-9(])")
_NUM_RE = re.compile(r"(?<![A-Za-z_0-9.])(\d+(?:\.\d+)?)(?![A-Za-z_0-9])")


def count_operators(formula: str) -> int:
    """算子调用数（`name(` 出现次数）。"""
    return len(_CALL_RE.findall(formula or ""))


def count_nesting(formula: str) -> int:
    """最大嵌套深度（按括号层级，根层为 0）。"""
    depth = 0
    max_depth = 0
    for ch in formula or "":
        if ch == "(":
            depth += 1
            max_depth = max(max_depth, depth - 1)
        elif ch == ")":
            depth = max(0, depth - 1)
    return max_depth


def referenced_fields(formula: str) -> list[str]:
    """公式引用的字段名（去重、保序；排除算子名与窗口数字）。"""
    funcs = set(_CALL_RE.findall(formula or ""))
    out: list[str] = []
    for name in _FIELD_RE.findall(formula or ""):
        if name in funcs or name in ("true", "false"):
            continue
        if name not in FIELD_TYPES:
            continue
        if name not in out:
            out.append(name)
    return out


def ast_structure_key(formula: str) -> str:
    """AST 结构键：**把数字字面量替换成 `#`**，其余原样。

    相同结构 = 只参数不同（向导 §6.3.6 第 8 条「同结构限变体」的判定键）。
    例：`mean(close,5)/mean(close,20)` 与 `mean(close,10)/mean(close,60)`
    结构键相同（都是 `mean(close,#)/mean(close,#)`）。
    """
    return _NUM_RE.sub("#", (formula or "").strip())


def structure_similarity(a: str, b: str) -> float:
    """两个公式的结构相似度 ∈ [0,1]：算子多重集 + 字段多重集 的 Jaccard 均值。

    用于多样性排序（向导 §6.3.6 第 11 条）：**相似度低者优先入库**。
    """
    def _tokens(formula: str) -> tuple[set[str], set[str]]:
        ops = {f"{name}(" for name in _CALL_RE.findall(formula or "")}
        flds = set(referenced_fields(formula))
        return ops, flds

    a_ops, a_flds = _tokens(a)
    b_ops, b_flds = _tokens(b)
    if not a_ops and not b_ops and not a_flds and not b_flds:
        return 1.0

    def _jaccard(x: set[str], y: set[str]) -> float:
        if not x and not y:
            return 1.0
        union = x | y
        return (len(x & y) / len(union)) if union else 1.0

    return round((_jaccard(a_ops, b_ops) + _jaccard(a_flds, b_flds)) / 2.0, 6)


# ══════════════════════════════════════════════════════════
# 配置与结果
# ══════════════════════════════════════════════════════════


@dataclass
class RandomGeneratorConfig:
    """生成配置（全部有默认值，便于调用方只填目标数量）。"""

    target_count: int
    seed: int = 42
    redundancy: float = REDUNDANCY_FACTOR
    max_operators: int = MAX_OPERATORS
    max_nesting: int = MAX_NESTING
    max_field_refs: int = MAX_FIELD_REFS
    max_variants_per_structure: int = MAX_VARIANTS_PER_STRUCTURE
    max_attempts_per_formula: int = MAX_ATTEMPTS_PER_FORMULA
    window_min: int = WINDOW_MIN
    window_max: int = WINDOW_MAX
    #: AI 生成不足时的补位数量（需求 §6.1 数量链路）
    backfill_count: int = 0

    @property
    def raw_target(self) -> int:
        """含冗余的生成目标（向导 §6.3.6 第 9 条）。"""
        return int(max(0, self.target_count) * max(1.0, self.redundancy))

    @property
    def final_target(self) -> int:
        """最终取 Top 的数量 = 目标 + 补位。"""
        return max(0, self.target_count) + max(0, self.backfill_count)


@dataclass
class RandomGenerationResult:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    notes_zh: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"candidates": self.candidates, "stats": dict(self.stats),
                "notes_zh": list(self.notes_zh)}


# ══════════════════════════════════════════════════════════
# 受约束生成
# ══════════════════════════════════════════════════════════


@dataclass
class _GenState:
    ops_used: int = 0
    fields_used: list[str] = field(default_factory=list)

    def add_field(self, name: str) -> None:
        if name not in self.fields_used:
            self.fields_used.append(name)


def _pick_window(cfg: RandomGeneratorConfig, rng: random.Random) -> int:
    pool = [w for w in WINDOW_POOL if cfg.window_min <= w <= cfg.window_max]
    if not pool:
        return rng.randint(cfg.window_min, cfg.window_max)
    return rng.choice(pool)


def _gen_node(
    *, cfg: RandomGeneratorConfig, rng: random.Random, state: _GenState,
    fields_by_type: Mapping[str, list[str]], depth: int,
    want_type: str | None = None, allow_operator: bool = True,
) -> tuple[str, str]:
    """递归生成 `(表达式, 类型)`。

    `depth` 从 0 起；`depth > max_nesting` 时强制叶子（保证嵌套上限）。
    返回类型可能是 `dimensionless`。
    """
    types_available = [t for t in ALL_TYPES if fields_by_type.get(t)]
    if not types_available:
        raise ValueError("没有可用字段（已选字段与算子类型表无交集）。")

    can_use_op = (
        allow_operator
        and state.ops_used < cfg.max_operators
        and depth <= cfg.max_nesting
    )
    # 叶子概率：越深越倾向叶子；字段引用已满也强制叶子
    fields_full = len(state.fields_used) >= cfg.max_field_refs
    if not can_use_op or fields_full or rng.random() < (0.25 + 0.2 * depth):
        return _gen_leaf(rng=rng, state=state, fields_by_type=fields_by_type,
                         want_type=want_type)

    # 选算子（受 want_type 约束：优先能产出该类型的算子）
    candidates = _operators_producing(want_type) if want_type else list(OP_SIGNATURES)
    if not candidates:
        candidates = list(OP_SIGNATURES)
    sig = OP_SIGNATURES[rng.choice(candidates)]

    # 二元运算符分支（`+`/`-`/`*`/`/`）
    if rng.random() < 0.35:
        return _gen_binary(cfg=cfg, rng=rng, state=state,
                           fields_by_type=fields_by_type, depth=depth,
                           want_type=want_type)

    state.ops_used += 1
    parts: list[str] = []
    arg_types: list[str] = []
    for slot in sig.inputs:
        if slot == "window":
            parts.append(str(_pick_window(cfg, rng)))
            arg_types.append("window")
            continue
        child_want = want_type if slot == "same" and want_type else (
            None if slot == "any" else slot)
        child_expr, child_type = _gen_node(
            cfg=cfg, rng=rng, state=state, fields_by_type=fields_by_type,
            depth=depth + 1, want_type=child_want)
        parts.append(child_expr)
        arg_types.append(child_type)

    out_type = sig.resolve_output(arg_types)
    return f"{sig.name}({','.join(parts)})", out_type


def _gen_leaf(*, rng: random.Random, state: _GenState,
              fields_by_type: Mapping[str, list[str]],
              want_type: str | None) -> tuple[str, str]:
    types = [t for t in ALL_TYPES if fields_by_type.get(t)]
    if want_type and fields_by_type.get(want_type):
        types = [want_type]
    t = rng.choice(types)
    name = rng.choice(fields_by_type[t])
    state.add_field(name)
    return name, t


def _gen_binary(*, cfg: RandomGeneratorConfig, rng: random.Random,
                state: _GenState, fields_by_type: Mapping[str, list[str]],
                depth: int, want_type: str | None) -> tuple[str, str]:
    """二元组合：`+`/`-` 同类型；`*`/`/` 至少一侧无量纲（或同类型相除→无量纲）。"""
    op = rng.choice(BINARY_OPS)
    if op in ("+", "-"):
        base = want_type or rng.choice(
            [t for t in ALL_TYPES if fields_by_type.get(t)])
        left, lt = _gen_node(cfg=cfg, rng=rng, state=state,
                             fields_by_type=fields_by_type, depth=depth + 1,
                             want_type=base)
        right, rt = _gen_node(cfg=cfg, rng=rng, state=state,
                              fields_by_type=fields_by_type, depth=depth + 1,
                              want_type=lt if lt in fields_by_type else base)
        if rt != lt:
            # 类型不匹配 → 退化为无量纲组合（`*`），保证类型安全
            return f"{left}*{right}", T_DIMENSIONLESS
        state.ops_used += 1
        return f"({left}{op}{right})", lt

    # `*` / `/`
    state.ops_used += 1
    left, lt = _gen_node(cfg=cfg, rng=rng, state=state,
                         fields_by_type=fields_by_type, depth=depth + 1,
                         want_type=want_type)
    if lt == T_DIMENSIONLESS:
        right, _rt = _gen_node(cfg=cfg, rng=rng, state=state,
                               fields_by_type=fields_by_type, depth=depth + 1,
                               want_type=want_type)
        return f"{left}*{right}", want_type or T_DIMENSIONLESS
    # 同类型相除 → 无量纲
    right, rt = _gen_node(cfg=cfg, rng=rng, state=state,
                          fields_by_type=fields_by_type, depth=depth + 1,
                          want_type=lt)
    if rt == lt:
        return f"{left}/{right}", T_DIMENSIONLESS
    return f"{left}*{right}", T_DIMENSIONLESS


def _operators_producing(want_type: str) -> list[str]:
    out: list[str] = []
    for name, sig in OP_SIGNATURES.items():
        if sig.output == want_type or sig.output == "same":
            out.append(name)
    return out


def generate_formula(*, cfg: RandomGeneratorConfig, rng: random.Random,
                     fields_by_type: Mapping[str, list[str]]) -> str:
    """生成**单个**受约束公式（不看复杂度校验，由调用方验）。"""
    expr, _t = _gen_node(cfg=cfg, rng=rng, state=_GenState(),
                         fields_by_type=fields_by_type, depth=0)
    return expr


def within_complexity_limits(formula: str, *,
                             cfg: RandomGeneratorConfig) -> tuple[bool, str]:
    """复杂度三重校验（向导 §6.3.6 第 4 条）。"""
    n_ops = count_operators(formula)
    if n_ops > cfg.max_operators:
        return False, f"算子数 {n_ops} > 上限 {cfg.max_operators}"
    depth = count_nesting(formula)
    if depth > cfg.max_nesting:
        return False, f"嵌套 {depth} > 上限 {cfg.max_nesting}"
    n_fields = len(referenced_fields(formula))
    if n_fields > cfg.max_field_refs:
        return False, f"字段引用 {n_fields} > 上限 {cfg.max_field_refs}"
    if n_ops == 0:
        return False, "无算子（纯字段不是合格因子）"
    return True, ""


# ══════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════


def generate_random_candidates(
    *, cfg: RandomGeneratorConfig,
    selected_fields: Iterable[str] | None = None,
    existing_formulas: Iterable[str] | None = None,
    existing_hashes: Iterable[str] | None = None,
    compiler: Callable[..., Any] | None = None,
    reference_formulas: Iterable[str] | None = None,
) -> RandomGenerationResult:
    """受约束随机生成主入口（向导 §6.3.6 第 6~12 条）。

    Args:
        selected_fields: 用户已选字段（只能从中取叶子）
        existing_formulas: 已有候选的公式（经典 + AI + 之前的随机），
            用于结构键统计、结构变体限流与多样性排序
        existing_hashes: 已有候选的哈希（**实时哈希拦截**用）
        compiler: 可注入编译器；缺省用 `factor_compiler.compile_formula`
        reference_formulas: 多样性排序的参照集（缺省用 existing_formulas）
    """
    if compiler is None:
        from app.services.factors.factor_compiler import compile_formula

        compiler = lambda *, formula, params=None: compile_formula(  # noqa: E731
            formula=formula, params=params)

    fields_by_type = available_fields_by_type(selected_fields)
    notes: list[str] = []
    if not fields_by_type:
        return RandomGenerationResult(
            stats={"generated": 0, "accepted": 0, "raw_target": cfg.raw_target},
            notes_zh=["没有可用字段（已选字段为空或其类型不在算子类型表内），"
                      "随机生成跳过。"])

    # ── 已有的哈希与结构统计（拦截 + 限变体）──
    seen_hashes: set[str] = {str(h) for h in (existing_hashes or [])}
    for f in (existing_formulas or []):
        seen_hashes.add(_formula_hash(str(f)))
    structure_counts: dict[str, int] = {}
    for f in (existing_formulas or []):
        key = ast_structure_key(str(f))
        structure_counts[key] = structure_counts.get(key, 0) + 1

    rng = random.Random(int(cfg.seed))
    raw_target = cfg.raw_target

    stats: dict[str, Any] = {
        "raw_target": raw_target,
        "attempts": 0,
        "generated": 0,
        "rejected_complexity": 0,
        "rejected_hash_collision": 0,
        "rejected_variant_cap": 0,
        "rejected_compile": 0,
        "rejected_duplicate_in_batch": 0,
    }
    accepted: list[dict[str, Any]] = []

    max_attempts = max(raw_target * cfg.max_attempts_per_formula,
                       cfg.max_attempts_per_formula)
    while len(accepted) < raw_target and stats["attempts"] < max_attempts:
        stats["attempts"] += 1
        try:
            formula = generate_formula(cfg=cfg, rng=rng,
                                       fields_by_type=fields_by_type)
        except ValueError:
            break
        stats["generated"] += 1

        ok, _why = within_complexity_limits(formula, cfg=cfg)
        if not ok:
            stats["rejected_complexity"] += 1
            continue

        # 实时哈希拦截（第 7 条）
        h = _formula_hash(formula)
        if h in seen_hashes:
            stats["rejected_hash_collision"] += 1
            continue

        # 同结构限变体（第 8 条）
        key = ast_structure_key(formula)
        if structure_counts.get(key, 0) >= cfg.max_variants_per_structure:
            stats["rejected_variant_cap"] += 1
            continue

        # 编译校验（第 6 条：类型/语法/AST）
        result = compiler(formula=formula, params=None)
        if not (getattr(result, "success", False)
                and getattr(result, "execution_plan", None) is not None):
            stats["rejected_compile"] += 1
            continue

        seen_hashes.add(h)
        structure_counts[key] = structure_counts.get(key, 0) + 1
        accepted.append(_to_candidate(formula, result.execution_plan))

    # ── 多样性排序（第 11 条）→ 取 Top（第 12 条）──
    reference = list(reference_formulas or existing_formulas or [])
    ordered = diversity_order(accepted, reference_formulas=reference)
    final = ordered[:cfg.final_target]
    stats["accepted"] = len(accepted)
    stats["selected"] = len(final)
    stats["backfill_count"] = cfg.backfill_count

    if len(final) < cfg.final_target:
        notes.append(
            f"受约束随机实际产出 {len(final)} 个（目标 {cfg.final_target}）—— "
            "**不足不硬凑**：复杂度/类型/哈希三重约束下无更多合法组合时如实返回。")

    return RandomGenerationResult(candidates=final, stats=stats, notes_zh=notes)


def _formula_hash(formula: str) -> str:
    """公式哈希（哈希拦截用）。

    **复用 `config_hash.compute_config_hash`**，不另起一套实现
    （R28：同一算法只能有一处实现，否则静默分叉）。
    T19 若引入专门的 `formula_hash`（含交换律归一），此处改为 import 它。
    """
    return CH.compute_config_hash({"formula": (formula or "").strip()})


def _to_candidate(formula: str, plan: Any) -> dict[str, Any]:
    return {
        "template_name": None,
        "formula": formula,
        "canonical_formula": getattr(plan, "formula", formula),
        "params": {},
        "required_fields": referenced_fields(formula),
        "complexity": float(getattr(plan, "complexity_score", 0.0)),
        "node_count": int(getattr(plan, "node_count", 0)),
        "ast_depth": int(getattr(plan, "ast_depth", 0)),
        "max_lookback": int(getattr(plan, "max_lookback", 1)),
        "direction": getattr(plan, "direction", "higher_better"),
        "operation": OPERATION_RANDOM,
        "source": SOURCE_RANDOM,
        "generation": 0,
        "formula_hash": _formula_hash(formula),
        "structure_key": ast_structure_key(formula),
        "compile_ok": True,
    }


def diversity_order(
    candidates: Sequence[Mapping[str, Any]],
    *, reference_formulas: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """按「与参照集相似度低者优先」排序（向导 §6.3.6 第 11 条）。

    相似度取候选与参照集中**最相似**者的分数（max），分数低→排前。

    **同分时的 tie-break 是「复杂度降序」**（不是升序）：
    随机生成的价值在探索人类/AI 没想到的组合（向导 §6.3.6「随机生成的作用」），
    而 `abs(roe_ttm)` 这类单算子式几乎等于原字段、信息量最低 ——
    若按复杂度升序，无参照集时（相似度全为 0）会把这类平凡式排在前面。
    最后以公式文本兜底，保证可复现。
    """
    refs = [str(f) for f in (reference_formulas or [])]

    def _max_similarity(formula: str) -> float:
        if not refs:
            return 0.0
        return max(structure_similarity(formula, r) for r in refs)

    scored: list[tuple[float, float, str, dict[str, Any]]] = []
    for c in candidates:
        row = dict(c)
        f = str(row.get("formula") or "")
        sim = _max_similarity(f)
        row["max_similarity_to_existing"] = sim
        row["operator_count"] = count_operators(f)
        scored.append((sim, -float(row.get("complexity") or 0.0), f, row))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [x[3] for x in scored]


__all__ = [
    "MAX_OPERATORS",
    "MAX_NESTING",
    "MAX_FIELD_REFS",
    "MAX_VARIANTS_PER_STRUCTURE",
    "MAX_ATTEMPTS_PER_FORMULA",
    "REDUNDANCY_FACTOR",
    "WINDOW_MIN",
    "WINDOW_MAX",
    "WINDOW_POOL",
    "SOURCE_RANDOM",
    "OPERATION_RANDOM",
    "T_PRICE",
    "T_VOLUME",
    "T_TURNOVER",
    "T_VALUATION",
    "T_QUALITY",
    "T_FLOW",
    "T_DIMENSIONLESS",
    "ALL_TYPES",
    "FIELD_TYPES",
    "OpSignature",
    "OP_SIGNATURES",
    "EXCLUDED_FUNCTIONS",
    "BINARY_OPS",
    "field_type",
    "available_fields_by_type",
    "count_operators",
    "count_nesting",
    "referenced_fields",
    "ast_structure_key",
    "structure_similarity",
    "RandomGeneratorConfig",
    "RandomGenerationResult",
    "generate_formula",
    "within_complexity_limits",
    "generate_random_candidates",
    "diversity_order",
]
