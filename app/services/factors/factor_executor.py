"""WP2-06: DuckDB 兼容执行（冻结批次读取、单写锁、失败不覆盖旧批次）。

将 WP2 编译器生成的 ExecutionPlan 在 DuckDB 仓库上执行：
1. 冻结批次读取：使用 read_only 连接读取源数据，获得一致快照
2. 公式评估：递归遍历预校验的 AST，在 pandas Series 上求值
3. 后处理：winsorize / zscore / rank / neutralize / missing_policy
4. 单写锁写入：使用 safe_write_context（跨进程锁 + 事务 ROLLBACK）
5. 失败不覆盖旧批次：calc_batch_id 作为主键一部分，新批次是新行

对齐 docs/专业因子库开发计划.md §WP2-06 和 docs/因子设置与专业因子库改造方案.md。

安全约束（参照 project_memory 硬约束）：
- AST 已由 FactorCompiler 预校验，执行器只遍历白名单节点
- ast.Pow 结果 > 1e100 返回 None（防 OOM）
- 滚动函数窗口参数已校验为正整数 ≤ 250
- 评估异常返回 None，不向外抛
"""
from __future__ import annotations

import ast
import math
import operator
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4
import uuid as _uuid

import pandas as pd

from app.services.factors.factor_compiler import (
    COMPILER_VERSION,
    ExecutionPlan,
    FIELD_CATALOG,
    FUNCTION_CATALOG,
    POW_RESULT_LIMIT,
)
from app.services.factors.store import FactorWarehouse


# ══════════════════════════════════════════════════════════
# 执行结果
# ══════════════════════════════════════════════════════════


@dataclass
class ExecutionOutcome:
    """因子执行结果。"""

    calc_batch_id: str
    rows_written: int
    eligible_rows: int
    trade_date_count: int
    symbol_count: int
    coverage: float
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calc_batch_id": self.calc_batch_id,
            "rows_written": self.rows_written,
            "eligible_rows": self.eligible_rows,
            "trade_date_count": self.trade_date_count,
            "symbol_count": self.symbol_count,
            "coverage": round(self.coverage, 6),
            "errors": self.errors,
        }


@dataclass
class PreviewOutcome:
    """因子预览结果（只读，不写入）。"""

    values: list[dict[str, Any]]
    trade_date: str | None
    symbol_count: int
    coverage: float
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": self.values,
            "trade_date": self.trade_date,
            "symbol_count": self.symbol_count,
            "coverage": round(self.coverage, 6),
            "errors": self.errors,
        }


@dataclass
class PanelExecutionOutcome:
    """面板批量执行结果（多交易日×多标的，长表格式）。"""

    factors_long: pd.DataFrame
    trade_dates: list[date]
    symbols: list[str]
    n_cells: int
    read_errors: list[dict] = field(default_factory=list)


# ══════════════════════════════════════════════════════════
# 公式评估器（在 pandas Series 上递归求值 AST）
# ══════════════════════════════════════════════════════════

# 二元运算符映射
_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}

# 一元运算符映射
_UNARY_OPS: dict[type, Any] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
}

# 比较运算符映射
_COMPARE_OPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _safe_pow(base: Any, exp: Any) -> Any:
    """安全幂运算，防 OOM。

    参照 indicator_ast_sandbox._safe_pow：
    - 指数绝对值 > 1000 拒绝
    - 预检查 |exp * log(|base|)| 超过 log(POW_RESULT_LIMIT) 拒绝
    - 结果 > POW_RESULT_LIMIT 返回 None
    """
    try:
        exp_val = float(exp) if not isinstance(exp, pd.Series) else exp
        if isinstance(exp_val, (int, float)) and abs(exp_val) > 1000:
            return None
        if isinstance(base, (int, float)) and isinstance(exp_val, (int, float)):
            if base != 0:
                log_result = abs(exp_val * math.log(abs(base)))
                if log_result > math.log(POW_RESULT_LIMIT):
                    return None
        result = base ** exp
        # 检查标量结果是否超限
        if isinstance(result, (int, float)):
            if math.isnan(result) or math.isinf(result):
                return None
            if abs(result) > POW_RESULT_LIMIT:
                return None
        return result
    except Exception:
        return None


def _eval_math_func(func_name: str, args: list[Any]) -> Any:
    """评估数学函数。"""
    if func_name == "abs":
        return args[0].abs() if isinstance(args[0], pd.Series) else abs(args[0])
    if func_name == "min":
        result = args[0]
        for a in args[1:]:
            result = result.combine(a, min) if isinstance(result, pd.Series) else min(result, a)
        return result
    if func_name == "max":
        result = args[0]
        for a in args[1:]:
            result = result.combine(a, max) if isinstance(result, pd.Series) else max(result, a)
        return result
    if func_name == "round":
        if len(args) == 2:
            return args[0].round(int(args[1])) if isinstance(args[0], pd.Series) else round(args[0], int(args[1]))
        return args[0].round() if isinstance(args[0], pd.Series) else round(args[0])
    if func_name == "log":
        return args[0].apply(lambda x: math.log(x) if x is not None and x > 0 else None) if isinstance(args[0], pd.Series) else (math.log(args[0]) if args[0] is not None and args[0] > 0 else None)
    if func_name == "sqrt":
        return args[0].apply(lambda x: math.sqrt(x) if x is not None and x >= 0 else None) if isinstance(args[0], pd.Series) else (math.sqrt(args[0]) if args[0] is not None and args[0] >= 0 else None)
    if func_name == "exp":
        return args[0].apply(lambda x: math.exp(x) if x is not None else None) if isinstance(args[0], pd.Series) else (math.exp(args[0]) if args[0] is not None else None)
    return None


def _eval_rolling_func(
    func_name: str,
    args: list[Any],
    *,
    symbol_series: pd.Series | None = None,
) -> Any:
    """评估滚动/时序函数。

    滚动函数约定：第一个参数为字段 Series，第二个参数为窗口大小。
    在调用前，调用方需将数据按 symbol 分组并按 trade_date 排序。
    """
    if len(args) < 2:
        return None

    field_data = args[0]
    window = int(args[1]) if not isinstance(args[1], pd.Series) else int(args[1].iloc[0])
    if window <= 0:
        return None

    if not isinstance(field_data, pd.Series):
        return None

    # 按 symbol 分组做滚动计算
    if symbol_series is not None:
        grouped = field_data.groupby(symbol_series)
    else:
        grouped = [(None, field_data)]

    results: list[pd.Series] = []
    for _, group in grouped:
        if func_name == "sma" or func_name == "mean":
            results.append(group.rolling(window=window, min_periods=1).mean())
        elif func_name == "ema":
            results.append(group.ewm(span=window, adjust=False).mean())
        elif func_name == "stddev":
            results.append(group.rolling(window=window, min_periods=2).std(ddof=0))
        elif func_name == "sum":
            results.append(group.rolling(window=window, min_periods=1).sum())
        elif func_name == "count":
            results.append(group.rolling(window=window, min_periods=1).count())
        elif func_name == "highest":
            results.append(group.rolling(window=window, min_periods=1).max())
        elif func_name == "lowest":
            results.append(group.rolling(window=window, min_periods=1).min())
        elif func_name == "ref":
            results.append(group.shift(window))
        elif func_name == "pct_change":
            shifted = group.shift(window)
            results.append((group - shifted) / shifted.abs().replace(0, math.nan))
        else:
            results.append(group)  # 未知函数返回原值

    if len(results) == 1:
        return results[0]
    return pd.concat(results)


# ══════════════════════════════════════════════════════════
# M2 / T03+T04+T05: 截面 / 时序算子的适配层
# ══════════════════════════════════════════════════════════
#
# 为什么需要适配层：
#   `_eval_ast` 的 context 是**按 source_data.index 对齐的 Series**
#   （`_evaluate` 里 `context[col] = source_data[col]`），
#   而 `dsl/cross_section.py` / `dsl/time_series.py` 要求
#   **MultiIndex(trade_date, symbol)** 的 DataFrame（那是 T03/T04 已冻结的契约）。
#   两者形状不同，故在此做转换：Series → 单列面板 → 调用 → 位置对齐回 Series。
#
# ⚠️ **不要**为了省这层适配去改 dsl 层的入参形状 ——
#    那会同时破坏 T03/T04 的契约与它们的测试。
#
# ⚠️ 分组轴不同，别写反：
#    cross_section 按 `trade_date`（MultiIndex level=0）分组；
#    timeseries    按 `symbol`（level=1）分组。

PANEL_TRADE_DATE_LEVEL = 0
PANEL_SYMBOL_LEVEL = 1


def _build_panel_index(source_data: pd.DataFrame) -> pd.MultiIndex | None:
    """由 source_data 的 trade_date / symbol 构造面板 MultiIndex（**保持行序**）。

    `source_data` 由 `_read_source_data` 读取，固定包含 `symbol` 与 `trade_date`
    两列（SQL 里显式 SELECT）。若缺失则返回 None —— 此时截面/时序算子无法求值。
    """
    if "trade_date" not in source_data.columns or "symbol" not in source_data.columns:
        return None
    return pd.MultiIndex.from_arrays(
        [source_data["trade_date"], source_data["symbol"]],
        names=["trade_date", "symbol"],
    )


def _coerce_periods(arg: Any) -> int | None:
    """把算子第 2 个参数（n / 窗口）规整为**正整数** int；非法返回 None。

    与 `_eval_rolling_func` 的取值方式一致（允许标量或 Series，Series 取首元素），
    但额外要求 **>= 1** —— 与 `time_series._validate_n` 和沙箱 `_positive_int` 同口径。
    返回 0/负数会让下游 `cs_quantile` 抛 ValueError，那会一路冒泡崩掉 `execute()`。
    """
    if isinstance(arg, pd.Series):
        if arg.empty:
            return None
        arg = arg.iloc[0]
    if isinstance(arg, bool) or not isinstance(arg, (int, float)):
        return None
    try:
        as_float = float(arg)
    except (TypeError, ValueError, OverflowError):
        return None
    if not as_float.is_integer():
        return None
    value = int(as_float)
    return value if value >= 1 else None


def _eval_panel_func(
    func_name: str,
    args: list[Any],
    *,
    category: str,
    panel_index: pd.MultiIndex | None,
) -> Any:
    """截面 / 时序算子求值：Series → 面板 DataFrame → 结果 Series。

    Args:
        func_name: 规范化算子名（如 `cs_rank` / `ts_atr`）
        args: 已求值的参数列表；`args[0]` 必须是 Series
        category: `cross_section` | `timeseries`
        panel_index: 由 `_build_panel_index` 构造；None 表示无法构造面板

    Returns:
        与 `args[0]` **位置对齐**的 Series；形状不匹配或参数非法时返回 None
        （与其他求值分支的失败约定一致：返回 None，由 `_evaluate` 统一落成 NaN）。
    """
    if not args or panel_index is None:
        return None

    values = args[0]
    if not isinstance(values, pd.Series):
        return None
    if len(values) != len(panel_index):
        # 形状不一致说明调用链上有 bug，宁可返回 None 也不要错位计算
        return None

    from app.services.factors.dsl.cross_section import eval_cross_section
    from app.services.factors.dsl.time_series import eval_time_series

    # ⚠️ 必须用 `values.to_numpy()` 而不是直接传 Series：
    #    `pd.DataFrame(series, index=multiindex)` 会按**标签对齐**，
    #    而传入的 Series 是 `source_data` 的 RangeIndex、与面板 MultiIndex 标签不匹配，
    #    结果是**整列变 NaN**（实测踩过：所有 cs_/ts_ 算子静默全 NaN）。
    #    传 ndarray 才是**按位置**装填，这正是我们要的（两者行序本来就一致）。
    panel = pd.DataFrame({"value": values.to_numpy()}, index=panel_index)

    if category == "cross_section":
        # `n` 只对 cs_quantile / cs_winsorize 有意义，其余算子忽略
        periods = _coerce_periods(args[1]) if len(args) > 1 else None
        if len(args) > 1 and periods is None:
            return None
        try:
            result = eval_cross_section(func_name, panel, n=periods)
        except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
            # 兜底：编译期已拦下绝大多数非法入参（`window_arg_index` 校验），
            # 但 `n` 的「值域」下限（如 cs_quantile 要求 n>=2）只有 dsl 层知道。
            # 返回 None → `_evaluate` 落成 NaN，**绝不**把异常抛到 execute() 外。
            return None
    else:
        periods = _coerce_periods(args[1]) if len(args) > 1 else None
        if periods is None:
            return None
        try:
            result = eval_time_series(func_name, panel, n=periods)
        except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
            return None

    if not isinstance(result, pd.DataFrame) or result.shape[1] != 1:
        return None
    series = result.iloc[:, 0]
    if len(series) != len(values):
        return None
    # 面板算子（groupby/tranform/rank/clip）都不改变行序，故按位置回对齐。
    # 用 `values.index`（原始 index）而非面板 index，下游才能与其它 Series 运算。
    series.index = values.index
    return series


def _eval_ast(
    node: ast.AST,
    context: dict[str, Any],
    *,
    symbol_series: pd.Series | None = None,
    panel_index: pd.MultiIndex | None = None,
) -> Any:
    """递归评估 AST 节点。

    Args:
        node: AST 节点（已由 FactorCompiler 预校验）
        context: 字段名 → pandas Series 或标量
        symbol_series: 按索引对齐的 symbol Series（用于滚动函数分组）
        panel_index: 按行序对齐的 MultiIndex(trade_date, symbol)，
            供截面 / 时序算子构造面板。默认 None → 这两类算子返回 None
            （与新增参数前的行为一致，满足 R2「只加可选参数、默认值保持原行为」）

    Returns:
        pandas Series 或标量
    """
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, context, symbol_series=symbol_series, panel_index=panel_index)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        # 布尔常量
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        # 字段或参数
        return context.get(node.id)

    if isinstance(node, ast.BinOp):
        left = _eval_ast(node.left, context, symbol_series=symbol_series, panel_index=panel_index)
        right = _eval_ast(node.right, context, symbol_series=symbol_series, panel_index=panel_index)
        op_func = _BIN_OPS.get(type(node.op))
        if op_func is None:
            if isinstance(node.op, ast.Pow):
                return _safe_pow(left, right)
            return None
        try:
            return op_func(left, right)
        except Exception:
            return None

    if isinstance(node, ast.UnaryOp):
        operand = _eval_ast(node.operand, context, symbol_series=symbol_series, panel_index=panel_index)
        op_func = _UNARY_OPS.get(type(node.op))
        if op_func is None:
            return None
        try:
            return op_func(operand)
        except Exception:
            return None

    if isinstance(node, ast.Compare):
        left = _eval_ast(node.left, context, symbol_series=symbol_series, panel_index=panel_index)
        result = left
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_ast(comparator, context, symbol_series=symbol_series, panel_index=panel_index)
            op_func = _COMPARE_OPS.get(type(op))
            if op_func is None:
                return None
            try:
                result = op_func(result, right)
            except Exception:
                return None
        return result

    if isinstance(node, ast.BoolOp):
        values = [_eval_ast(v, context, symbol_series=symbol_series, panel_index=panel_index) for v in node.values]
        if isinstance(node.op, ast.And):
            result = values[0]
            for v in values[1:]:
                try:
                    result = result & v if isinstance(result, pd.Series) else (result and v)
                except Exception:
                    return None
            return result
        if isinstance(node.op, ast.Or):
            result = values[0]
            for v in values[1:]:
                try:
                    result = result | v if isinstance(result, pd.Series) else (result or v)
                except Exception:
                    return None
            return result
        return None

    if isinstance(node, ast.IfExp):
        test = _eval_ast(node.test, context, symbol_series=symbol_series, panel_index=panel_index)
        body = _eval_ast(node.body, context, symbol_series=symbol_series, panel_index=panel_index)
        orelse = _eval_ast(node.orelse, context, symbol_series=symbol_series, panel_index=panel_index)
        try:
            if isinstance(test, pd.Series):
                return test.where(test.astype(bool), orelse).where(~test.astype(bool), body)
            return body if test else orelse
        except Exception:
            return None

    if isinstance(node, ast.Call):
        func_name = node.func.id if isinstance(node.func, ast.Name) else "<unknown>"
        args = [_eval_ast(a, context, symbol_series=symbol_series, panel_index=panel_index) for a in node.args]
        spec = FUNCTION_CATALOG.get(func_name)
        if spec is None:
            return None
        if spec.category == "rolling":
            return _eval_rolling_func(func_name, args, symbol_series=symbol_series)
        if spec.category == "math":
            return _eval_math_func(func_name, args)
        # M2/T05 新增分支。⚠️ 两个都要有：
        #   cross_section → cs_* 族（按 trade_date 分组）
        #   timeseries    → ts_* 族（按 symbol 分组）
        # 漏任一个都会落到下面的 `return None` → 因子值**全 NaN 且不报错**。
        if spec.category in ("cross_section", "timeseries"):
            return _eval_panel_func(
                func_name,
                args,
                category=spec.category,
                panel_index=panel_index,
            )
        return None

    if isinstance(node, ast.Tuple):
        elts = [_eval_ast(e, context, symbol_series=symbol_series, panel_index=panel_index) for e in node.elts]
        return elts[0] if elts else None

    if isinstance(node, ast.List):
        return [_eval_ast(e, context, symbol_series=symbol_series, panel_index=panel_index) for e in node.elts]

    return None


# ══════════════════════════════════════════════════════════
# 后处理
# ══════════════════════════════════════════════════════════


def _winsorize_mad(series: pd.Series, multiplier: float = 3.0) -> pd.Series:
    """MAD winsorize（横截面）。"""
    valid = series.dropna()
    if len(valid) < 5:
        return series
    median = valid.median()
    mad = (valid - median).abs().median()
    if mad == 0 or math.isnan(mad):
        # MAD≈0 回退到分位数
        lower = valid.quantile(0.01)
        upper = valid.quantile(0.99)
    else:
        scale = 1.4826 * mad
        lower = median - multiplier * scale
        upper = median + multiplier * scale
    return series.clip(lower=lower, upper=upper)


def _winsorize_quantile(
    series: pd.Series, lower_q: float = 0.01, upper_q: float = 0.99
) -> pd.Series:
    """分位数 winsorize（横截面）。"""
    valid = series.dropna()
    if len(valid) < 5:
        return series
    lower = valid.quantile(lower_q)
    upper = valid.quantile(upper_q)
    return series.clip(lower=lower, upper=upper)


def _zscore(series: pd.Series, ddof: int = 0) -> pd.Series:
    """横截面 z-score。"""
    valid = series.dropna()
    if len(valid) < 2:
        return pd.Series(0.0, index=series.index)
    std = valid.std(ddof=ddof)
    if std == 0 or math.isnan(std):
        return pd.Series(0.0, index=series.index)
    mean = valid.mean()
    return (series - mean) / std


def _rank_percentile(series: pd.Series, ascending: bool = True) -> pd.Series:
    """百分位 rank（横截面）。

    直接对原 Series 做 rank，NaN 保持 NaN。
    """
    if len(series) == 0:
        return series
    return series.rank(pct=True, ascending=ascending)


#: 缺日线 spine 时的结构化错误码（保留 `no_source_data` 作为**向后兼容前缀**）
NO_SOURCE_DATA = "no_source_data"


def _no_spine_error(*, trade_date: Any, plan: Any) -> str:
    """缺日线 spine 时的**结构化、可操作**错误串（裁决 ③，2026-09-17）。

    为什么要有这条（而不是静默返回空）
    ----------------------------------
    `raw_daily_bars` 是因子计算的**对齐基准**（universe × date 的网格）：
    估值 / 财报类公式虽然只用 pe_ttm、roe_ttm 这类字段，也必须挂在日线网格上
    才能按交易日对齐、算覆盖率、与目标收益对齐。

    此前该分支只回一个裸串 `no_source_data`，调用方分不清是「今天没数据」
    还是「从未镜像过行情」。现在给出：缺哪张表、哪个日期、依赖了哪些字段、
    以及**去哪儿修**（数据中心 → 历史行情镜像）。

    ⚠️ 保持返回 `errors=[...]`（不改成抛异常）：这是 L1 既有契约，
    调用方按 `outcome.errors` 判断；改抛异常会破坏既有调用面（R2）。
    """
    deps = ""
    try:
        fields = (plan.data_dependencies or {}).get("fields") or []
        if fields:
            deps = f"（本公式依赖字段：{', '.join(str(f) for f in fields)}）"
    except Exception:  # noqa: BLE001 - 依赖信息只用于提示，取不到也不影响报错
        deps = ""
    when = f"{trade_date}" if trade_date else "全区间"
    return (
        f"{NO_SOURCE_DATA}: 缺少日线数据用于对齐（raw_daily_bars 在 {when} 无数据）"
        f"{deps}。估值/财报类公式同样需要日线作为**交易日网格基准**；"
        "请先在「设置 → 数据中心 → 历史行情镜像」补该区间行情后重试。"
    )


def apply_postprocess(
    values: pd.Series,
    postprocess: dict[str, Any] | None,
) -> tuple[pd.Series, pd.Series]:
    """应用后处理配置，返回 (winsorized, normalized)。

    处理顺序：winsorize → zscore/rank → missing_policy
    """
    if postprocess is None:
        return values, values

    winsorized = values.copy()

    # 1. winsorize
    winsor_cfg = postprocess.get("winsorize")
    if winsor_cfg and isinstance(winsor_cfg, dict):
        method = winsor_cfg.get("method", "mad")
        if method == "mad":
            winsorized = _winsorize_mad(
                winsorized, multiplier=winsor_cfg.get("mad_multiplier", 3.0)
            )
        elif method == "quantile":
            winsorized = _winsorize_quantile(
                winsorized,
                lower_q=winsor_cfg.get("lower_quantile", 0.01),
                upper_q=winsor_cfg.get("upper_quantile", 0.99),
            )

    # 2. zscore 或 rank
    normalized = winsorized.copy()
    zscore_cfg = postprocess.get("zscore")
    rank_cfg = postprocess.get("rank")

    if zscore_cfg and isinstance(zscore_cfg, dict):
        normalized = _zscore(normalized, ddof=zscore_cfg.get("ddof", 0))
    elif rank_cfg and isinstance(rank_cfg, dict):
        ascending = rank_cfg.get("ascending", True)
        normalized = _rank_percentile(normalized, ascending=ascending)
    else:
        # 默认 zscore
        normalized = _zscore(normalized, ddof=0)

    # 3. missing_policy
    policy = postprocess.get("missing_policy", "exclude")
    if policy == "impute_zero":
        winsorized = winsorized.fillna(0.0)
        normalized = normalized.fillna(0.0)
    # "exclude" 和 "ignore" 保持 NaN

    return winsorized, normalized


def apply_postprocess_cross_sectional(
    raw_values: pd.Series,
    trade_date_series: pd.Series,
    postprocess: dict[str, Any] | None,
) -> tuple[pd.Series, pd.Series]:
    """横截面后处理（按 trade_date 分组独立执行 winsorize/zscore/rank）。

    Args:
        raw_values: 完整历史上的 raw 因子值（与 trade_date_series 按索引对齐）
        trade_date_series: 对应每一行的 trade_date（用于分组）
        postprocess: 后处理配置

    Returns:
        (winsorized, normalized) 与输入索引对齐的 Series
    """
    if postprocess is None:
        return raw_values.copy(), raw_values.copy()

    winsorized = pd.Series(index=raw_values.index, dtype=float)
    normalized = pd.Series(index=raw_values.index, dtype=float)

    for _, idx in raw_values.groupby(trade_date_series).groups.items():
        group_raw = raw_values.loc[idx]
        group_w, group_n = apply_postprocess(group_raw, postprocess)
        winsorized.loc[idx] = group_w.values
        normalized.loc[idx] = group_n.values

    return winsorized, normalized


# ══════════════════════════════════════════════════════════
# 因子执行器
# ══════════════════════════════════════════════════════════


class FactorExecutor:
    """因子执行器（WP2-06）：DuckDB 兼容执行。

    用法：
        executor = FactorExecutor(warehouse)
        # 预览（只读）
        outcome = executor.preview(plan, trade_date=date(2026, 7, 24))
        # 执行（写入）
        outcome = executor.execute(plan, factor_code="my_factor", factor_version=1)

    安全保障：
    - 冻结批次读取：read_only 连接获取一致快照
    - 单写锁：safe_write_context（跨进程锁 + 事务）
    - 失败不覆盖旧批次：calc_batch_id 作为主键 + ROLLBACK
    """

    def __init__(self, warehouse: FactorWarehouse):
        self.warehouse = warehouse

    @staticmethod
    def _select_field_expressions(table_name: str, table_fields: list[str]) -> list[str]:
        """Return safe SQL projections, including virtual warehouse fields."""
        expressions: list[str] = []
        for field_name in table_fields:
            if table_name == "raw_daily_bars" and field_name == "prev_close":
                expressions.append(
                    "LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close"
                )
            else:
                expressions.append(field_name)
        return expressions

    @staticmethod
    def _merge_financial_asof(
        spine: pd.DataFrame,
        financial: pd.DataFrame,
    ) -> pd.DataFrame:
        """PIT-join financial fields using the latest announced report.

        ``announcement_date <= trade_date`` is the core future-leakage guard.
        Multiple reports announced on the same date are reduced to the latest
        report period before the as-of merge.
        """
        if spine.empty or financial.empty:
            return spine
        left = spine.copy()
        right = financial.copy()
        left["trade_date"] = pd.to_datetime(left["trade_date"], errors="coerce")
        right["announcement_date"] = pd.to_datetime(
            right["announcement_date"], errors="coerce"
        )
        left = left.dropna(subset=["symbol", "trade_date"])
        right = right.dropna(subset=["symbol", "announcement_date"])
        if "report_period" in right.columns:
            right["report_period"] = pd.to_datetime(
                right["report_period"], errors="coerce"
            )
            right = right.sort_values(
                ["symbol", "announcement_date", "report_period"]
            ).drop_duplicates(["symbol", "announcement_date"], keep="last")
        else:
            right = right.drop_duplicates(["symbol", "announcement_date"], keep="last")

        # pandas merge_asof requires the join key to be globally monotonic.
        left = left.sort_values(["trade_date", "symbol"])
        right = right.sort_values(["announcement_date", "symbol"])
        merged = pd.merge_asof(
            left,
            right,
            left_on="trade_date",
            right_on="announcement_date",
            by="symbol",
            direction="backward",
            allow_exact_matches=True,
        )
        merged = merged.drop(
            columns=[column for column in ("announcement_date", "report_period") if column in merged.columns]
        )
        merged["trade_date"] = merged["trade_date"].dt.date
        return merged.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    @staticmethod
    def _merge_valuation_asof(
        spine: pd.DataFrame,
        valuation: pd.DataFrame,
        *,
        max_age_days: int = 7,
    ) -> pd.DataFrame:
        """Join the latest valuation snapshot without looking into the future."""
        if spine.empty or valuation.empty:
            return spine
        left = spine.copy()
        right = valuation.copy()
        left["trade_date"] = pd.to_datetime(left["trade_date"], errors="coerce")
        right["trade_date"] = pd.to_datetime(right["trade_date"], errors="coerce")
        left = left.dropna(subset=["symbol", "trade_date"])
        right = right.dropna(subset=["symbol", "trade_date"])
        right = right.sort_values(["symbol", "trade_date"]).drop_duplicates(
            ["symbol", "trade_date"], keep="last"
        )
        left = left.sort_values(["trade_date", "symbol"])
        right = right.sort_values(["trade_date", "symbol"])
        merged = pd.merge_asof(
            left,
            right,
            on="trade_date",
            by="symbol",
            direction="backward",
            tolerance=pd.Timedelta(days=max_age_days),
            allow_exact_matches=True,
        )
        merged["trade_date"] = merged["trade_date"].dt.date
        return merged.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    # ── 冻结批次读取 ──────────────────────────────────────

    def _read_source_data(
        self,
        plan: ExecutionPlan,
        *,
        trade_date: date | None = None,
        lookback_days: int = 30,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """冻结批次读取源数据（read_only 连接）。

        根据 plan.data_dependencies 读取所需字段，返回按 (symbol, trade_date) 排序的 DataFrame。
        如果指定 trade_date，则只读取该日期及之前 lookback_days + max_lookback 天的数据。

        Returns:
            (DataFrame, structured_errors)
        """
        structured_errors: list[dict[str, Any]] = []
        deps = plan.data_dependencies
        fields = deps.get("fields", [])
        source_tables = deps.get("source_tables", [])
        max_lookback = deps.get("max_lookback", 1)

        if not fields or not source_tables:
            return pd.DataFrame(), structured_errors

        # 构建字段到表名的映射
        field_to_table: dict[str, str] = {}
        for field_name in fields:
            spec = FIELD_CATALOG.get(field_name)
            if spec:
                field_to_table[field_name] = spec.source_table

        if not field_to_table:
            return pd.DataFrame(), structured_errors

        # 计算读取日期范围
        range_start: date | pd.Timestamp | None = None
        range_end: date | None = trade_date
        if trade_date is not None:
            # 读足够多的历史数据用于滚动计算
            history_days = max(lookback_days, max_lookback + 10)
            range_start = trade_date - pd.Timedelta(days=history_days)
            date_clause = (
                f"trade_date >= '{range_start}' AND trade_date <= '{trade_date}'"
            )
        else:
            date_clause = ""

        # 冻结批次读取：read_only 连接
        try:
            with self.warehouse.connection(read_only=True) as conn:
                frames: dict[str, pd.DataFrame] = {}
                financial_frame = pd.DataFrame()
                valuation_frame = pd.DataFrame()
                for table_name in set(field_to_table.values()):
                    table_fields = [
                        f for f, t in field_to_table.items() if t == table_name
                    ]
                    if table_name == "raw_financial_reports":
                        financial_conditions: list[str] = []
                        if range_end is not None:
                            financial_conditions.append(
                                f"announcement_date <= '{range_end}'"
                            )
                        financial_where = (
                            f"WHERE {' AND '.join(financial_conditions)}"
                            if financial_conditions
                            else ""
                        )
                        sql = (
                            "SELECT symbol, announcement_date, report_period, "
                            f"{', '.join(table_fields)} FROM {table_name} {financial_where}"
                        )
                        try:
                            financial_frame = conn.execute(sql).fetchdf()
                        except Exception as exc:
                            structured_errors.append({
                                "source_table": table_name,
                                "required_fields": list(table_fields),
                                "category": "sql",
                                "correlation_id": _uuid.uuid4().hex[:8],
                                "message": f"{type(exc).__name__}: {exc}",
                                "sql": sql,
                            })
                        continue
                    if table_name == "raw_valuation_snapshots":
                        valuation_conditions: list[str] = []
                        if range_end is not None:
                            valuation_conditions.append(f"trade_date <= '{range_end}'")
                        valuation_where = (
                            f"WHERE {' AND '.join(valuation_conditions)}"
                            if valuation_conditions
                            else ""
                        )
                        sql = (
                            f"SELECT symbol, trade_date, {', '.join(table_fields)} "
                            f"FROM {table_name} {valuation_where}"
                        )
                        try:
                            valuation_frame = conn.execute(sql).fetchdf()
                        except Exception as exc:
                            structured_errors.append({
                                "source_table": table_name,
                                "required_fields": list(table_fields),
                                "category": "sql",
                                "correlation_id": _uuid.uuid4().hex[:8],
                                "message": f"{type(exc).__name__}: {exc}",
                                "sql": sql,
                            })
                        continue
                    # 构建 WHERE 子句
                    conditions: list[str] = []
                    if table_name == "raw_daily_bars":
                        conditions.append("adjust = 'qfq'")
                    if date_clause:
                        conditions.append(date_clause)
                    where_sql = (
                        f"WHERE {' AND '.join(conditions)}"
                        if conditions
                        else ""
                    )
                    select_fields = self._select_field_expressions(table_name, table_fields)
                    sql = (
                        f"SELECT symbol, trade_date, {', '.join(select_fields)} "
                        f"FROM {table_name} {where_sql}"
                    )
                    try:
                        df = conn.execute(sql).fetchdf()
                        if not df.empty:
                            frames[table_name] = df
                    except Exception as exc:
                        structured_errors.append({
                            "source_table": table_name,
                            "required_fields": list(table_fields),
                            "category": "sql",
                            "correlation_id": _uuid.uuid4().hex[:8],
                            "message": f"{type(exc).__name__}: {exc}",
                            "sql": sql,
                        })
                        continue

                if (not financial_frame.empty or not valuation_frame.empty) and "raw_daily_bars" not in frames:
                    spine_conditions = ["adjust = 'qfq'"]
                    if date_clause:
                        spine_conditions.append(date_clause)
                    spine_sql = (
                        "SELECT symbol, trade_date FROM raw_daily_bars "
                        f"WHERE {' AND '.join(spine_conditions)}"
                    )
                    try:
                        spine = conn.execute(spine_sql).fetchdf()
                        if not spine.empty:
                            frames["raw_daily_bars"] = spine
                    except Exception as exc:
                        structured_errors.append({
                            "source_table": "raw_daily_bars",
                            "required_fields": ["symbol", "trade_date"],
                            "category": "sql",
                            "correlation_id": _uuid.uuid4().hex[:8],
                            "message": f"{type(exc).__name__}: {exc}",
                            "sql": spine_sql,
                        })
        except Exception as exc:
            structured_errors.append({
                "source_table": None,
                "required_fields": list(fields),
                "category": "read",
                "correlation_id": _uuid.uuid4().hex[:8],
                "message": f"warehouse connection failed: {type(exc).__name__}: {exc}",
            })
            return pd.DataFrame(), structured_errors

        if not frames:
            return pd.DataFrame(), structured_errors

        # 合并所有表的数据（按 symbol + trade_date 外连接）
        exact_frames = list(frames.values())
        result = exact_frames[0]
        for df in exact_frames[1:]:
            result = result.merge(df, on=["symbol", "trade_date"], how="outer")

        if not financial_frame.empty:
            result = self._merge_financial_asof(result, financial_frame)
        if not valuation_frame.empty:
            result = self._merge_valuation_asof(result, valuation_frame)

        # 按 symbol, trade_date 排序
        result = result.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        return result, structured_errors

    # ── 公式评估 ──────────────────────────────────────────

    def _evaluate(
        self,
        plan: ExecutionPlan,
        source_data: pd.DataFrame,
    ) -> pd.Series:
        """在源数据上评估因子公式。

        Args:
            plan: 编译后的执行计划
            source_data: 冻结批次读取的源数据 DataFrame

        Returns:
            按源数据索引对齐的因子值 Series
        """
        if source_data.empty:
            return pd.Series(dtype=float)

        # 解析 AST
        tree = ast.parse(plan.formula, mode="eval")

        # 构建评估上下文：字段名 → Series
        context: dict[str, Any] = {}
        for col in source_data.columns:
            if col in ("symbol", "trade_date"):
                continue
            context[col] = source_data[col]

        # 添加参数
        for key, value in plan.params.items():
            if key not in context:
                context[key] = value

        # symbol Series 用于滚动函数分组
        symbol_series = source_data.get("symbol")

        # 面板索引（多日 × 多标的）供截面 / 时序算子构造 DataFrame（M2/T05）
        panel_index = _build_panel_index(source_data)

        # 递归评估 AST
        result = _eval_ast(tree, context, symbol_series=symbol_series, panel_index=panel_index)

        if result is None:
            return pd.Series(math.nan, index=source_data.index)
        if not isinstance(result, pd.Series):
            # 标量结果广播
            return pd.Series(result, index=source_data.index)

        return result

    # ── 预览（只读） ──────────────────────────────────────

    def preview(
        self,
        plan: ExecutionPlan,
        *,
        trade_date: date | None = None,
        limit: int = 20,
        symbols: list[str] | None = None,
    ) -> PreviewOutcome:
        """预览因子值（只读，不写入 DuckDB）。

        流程：
        1. 冻结批次读取源数据
        2. 评估公式
        3. 应用后处理
        4. 返回前 N 个结果
        """
        errors: list[str] = []

        # 1. 冻结批次读取
        try:
            source_data, read_errors = self._read_source_data(plan, trade_date=trade_date)
            for err in read_errors:
                errors.append(f"{err['category']}_error: [{err['correlation_id']}] {err['message']}")
        except Exception as exc:
            return PreviewOutcome(
                values=[],
                trade_date=str(trade_date) if trade_date else None,
                symbol_count=0,
                coverage=0.0,
                errors=[f"read_error: {exc}"],
            )

        if source_data.empty:
            return PreviewOutcome(
                values=[],
                trade_date=str(trade_date) if trade_date else None,
                symbol_count=0,
                coverage=0.0,
                errors=[_no_spine_error(trade_date=trade_date, plan=plan)],
            )

        if symbols:
            requested_symbols = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
            source_data = source_data[source_data["symbol"].astype(str).isin(requested_symbols)]

        if source_data.empty:
            return PreviewOutcome(
                values=[],
                trade_date=str(trade_date) if trade_date else None,
                symbol_count=0,
                coverage=0.0,
                errors=["no_data_for_trade_date"],
            )

        # 2. 在完整预热历史上评估公式，随后才截取目标交易日。
        try:
            raw_values = self._evaluate(plan, source_data)
        except Exception as exc:
            return PreviewOutcome(
                values=[],
                trade_date=str(trade_date) if trade_date else None,
                symbol_count=0,
                coverage=0.0,
                errors=[f"eval_error: {exc}"],
            )

        # 3. 后处理按交易日横截面执行，避免把不同日期混在一起。
        winsorized, normalized = apply_postprocess_cross_sectional(
            raw_values, source_data["trade_date"], plan.postprocess
        )

        if trade_date is not None:
            target_mask = source_data["trade_date"].astype(str) == str(trade_date)
        else:
            latest_trade_date = source_data["trade_date"].max()
            target_mask = source_data["trade_date"] == latest_trade_date
        source_data = source_data.loc[target_mask].reset_index(drop=True)
        raw_values = raw_values.loc[target_mask].reset_index(drop=True)
        winsorized = winsorized.loc[target_mask].reset_index(drop=True)
        normalized = normalized.loc[target_mask].reset_index(drop=True)

        if source_data.empty:
            return PreviewOutcome(
                values=[],
                trade_date=str(trade_date) if trade_date else None,
                symbol_count=0,
                coverage=0.0,
                errors=["no_data_for_trade_date"],
            )

        # 4. 构建预览值
        eligible = raw_values.notna()
        symbol_count = source_data["symbol"].nunique()
        coverage = float(eligible.sum()) / len(eligible) if len(eligible) > 0 else 0.0

        values: list[dict[str, Any]] = []
        for idx in range(min(limit, len(source_data))):
            symbol = source_data.iloc[idx].get("symbol")
            td = source_data.iloc[idx].get("trade_date")
            values.append({
                "symbol": symbol,
                "trade_date": str(td) if td is not None else None,
                "raw_value": _safe_float(raw_values.iloc[idx]) if idx < len(raw_values) else None,
                "winsorized_value": _safe_float(winsorized.iloc[idx]) if idx < len(winsorized) else None,
                "normalized_value": _safe_float(normalized.iloc[idx]) if idx < len(normalized) else None,
                "eligible": bool(eligible.iloc[idx]) if idx < len(eligible) else False,
            })

        return PreviewOutcome(
            values=values,
            trade_date=str(trade_date) if trade_date else str(source_data["trade_date"].iloc[-1]),
            symbol_count=symbol_count,
            coverage=coverage,
            errors=errors,
        )

    # ── 执行（写入） ──────────────────────────────────────

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        factor_code: str,
        factor_version: int = 1,
        trade_date: date | None = None,
        calc_batch_id: str | None = None,
    ) -> ExecutionOutcome:
        """执行因子公式并写入 factor_values 表。

        安全保障：
        - 冻结批次读取：read_only 连接获取一致快照
        - 单写锁：safe_write_context（跨进程锁 + 事务）
        - 失败不覆盖旧批次：calc_batch_id 作为主键 + ROLLBACK
        """
        batch_id = calc_batch_id or f"factor-{factor_code}-{uuid4().hex[:12]}"
        errors: list[str] = []

        # 1. 冻结批次读取
        try:
            source_data, read_errors = self._read_source_data(plan, trade_date=trade_date)
            for err in read_errors:
                errors.append(f"{err['category']}_error: [{err['correlation_id']}] {err['message']}")
        except Exception as exc:
            return ExecutionOutcome(
                calc_batch_id=batch_id,
                rows_written=0,
                eligible_rows=0,
                trade_date_count=0,
                symbol_count=0,
                coverage=0.0,
                errors=[f"read_error: {exc}"],
            )

        if source_data.empty:
            return ExecutionOutcome(
                calc_batch_id=batch_id,
                rows_written=0,
                eligible_rows=0,
                trade_date_count=0,
                symbol_count=0,
                coverage=0.0,
                errors=[_no_spine_error(trade_date=trade_date, plan=plan)],
            )

        # 2. 评估公式
        try:
            raw_values = self._evaluate(plan, source_data)
        except Exception as exc:
            return ExecutionOutcome(
                calc_batch_id=batch_id,
                rows_written=0,
                eligible_rows=0,
                trade_date_count=0,
                symbol_count=0,
                coverage=0.0,
                errors=[f"eval_error: {exc}"],
            )

        # 3. 后处理按交易日横截面执行；滚动公式仍使用完整预热历史。
        winsorized, normalized = apply_postprocess_cross_sectional(
            raw_values, source_data["trade_date"], plan.postprocess
        )
        if trade_date is not None:
            target_mask = source_data["trade_date"].astype(str) == str(trade_date)
            source_data = source_data.loc[target_mask].reset_index(drop=True)
            raw_values = raw_values.loc[target_mask].reset_index(drop=True)
            winsorized = winsorized.loc[target_mask].reset_index(drop=True)
            normalized = normalized.loc[target_mask].reset_index(drop=True)
            if source_data.empty:
                return ExecutionOutcome(
                    calc_batch_id=batch_id,
                    rows_written=0,
                    eligible_rows=0,
                    trade_date_count=0,
                    symbol_count=0,
                    coverage=0.0,
                    errors=["no_data_for_trade_date"],
                )
        eligible = raw_values.notna()

        # 4. 构建输出 DataFrame
        now = datetime.now(timezone.utc)
        output = pd.DataFrame({
            "symbol": source_data["symbol"],
            "trade_date": source_data["trade_date"],
            "factor_code": factor_code,
            "factor_version": factor_version,
            "raw_value": raw_values.values,
            "winsorized_value": winsorized.values,
            "normalized_value": normalized.values,
            "is_imputed": False,
            "imputation_method": None,
            "eligible": eligible.values,
            "data_cutoff_at": now,
            "calc_batch_id": batch_id,
            "created_at": now,
        })

        # 5. 单写锁写入（safe_write_context：跨进程锁 + 事务 ROLLBACK）
        try:
            with self.warehouse.safe_write_context() as conn:
                conn.register("incoming_factor_values", output)
                conn.execute("""
                    INSERT INTO factor_values
                    SELECT * FROM incoming_factor_values
                """)
        except Exception as exc:
            return ExecutionOutcome(
                calc_batch_id=batch_id,
                rows_written=0,
                eligible_rows=0,
                trade_date_count=0,
                symbol_count=0,
                coverage=0.0,
                errors=[f"write_error: {exc}"],
            )

        # 6. 统计
        rows_written = len(output)
        eligible_rows = int(eligible.sum())
        trade_date_count = source_data["trade_date"].nunique()
        symbol_count = source_data["symbol"].nunique()
        coverage = eligible_rows / rows_written if rows_written > 0 else 0.0

        return ExecutionOutcome(
            calc_batch_id=batch_id,
            rows_written=rows_written,
            eligible_rows=eligible_rows,
            trade_date_count=trade_date_count,
            symbol_count=symbol_count,
            coverage=coverage,
            errors=errors,
        )

    # ── 面板批量读取 ──────────────────────────────────────

    def _read_source_data_panel(
        self,
        plan: ExecutionPlan,
        *,
        start_date: date,
        end_date: date,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """读取 [start_date, end_date] 全量源数据（不分日、不设 limit）。

        Returns:
            (DataFrame 按 symbol, trade_date 排序, structured_errors)
        """
        structured_errors: list[dict[str, Any]] = []
        deps = plan.data_dependencies
        fields = deps.get("fields", [])
        source_tables = deps.get("source_tables", [])

        if not fields or not source_tables:
            return pd.DataFrame(), structured_errors

        # 构建字段到表名的映射
        field_to_table: dict[str, str] = {}
        for field_name in fields:
            spec = FIELD_CATALOG.get(field_name)
            if spec:
                field_to_table[field_name] = spec.source_table

        if not field_to_table:
            return pd.DataFrame(), structured_errors

        date_clause = (
            f"trade_date >= '{start_date}' AND trade_date <= '{end_date}'"
        )

        try:
            with self.warehouse.connection(read_only=True) as conn:
                frames: dict[str, pd.DataFrame] = {}
                financial_frame = pd.DataFrame()
                valuation_frame = pd.DataFrame()
                for table_name in set(field_to_table.values()):
                    table_fields = [
                        f for f, t in field_to_table.items() if t == table_name
                    ]
                    if table_name == "raw_financial_reports":
                        sql = (
                            "SELECT symbol, announcement_date, report_period, "
                            f"{', '.join(table_fields)} FROM {table_name} "
                            f"WHERE announcement_date <= '{end_date}'"
                        )
                        try:
                            financial_frame = conn.execute(sql).fetchdf()
                        except Exception as exc:
                            structured_errors.append({
                                "source_table": table_name,
                                "required_fields": list(table_fields),
                                "category": "sql",
                                "correlation_id": _uuid.uuid4().hex[:8],
                                "message": f"{type(exc).__name__}: {exc}",
                                "sql": sql,
                            })
                        continue
                    if table_name == "raw_valuation_snapshots":
                        sql = (
                            f"SELECT symbol, trade_date, {', '.join(table_fields)} "
                            f"FROM {table_name} WHERE trade_date <= '{end_date}'"
                        )
                        try:
                            valuation_frame = conn.execute(sql).fetchdf()
                        except Exception as exc:
                            structured_errors.append({
                                "source_table": table_name,
                                "required_fields": list(table_fields),
                                "category": "sql",
                                "correlation_id": _uuid.uuid4().hex[:8],
                                "message": f"{type(exc).__name__}: {exc}",
                                "sql": sql,
                            })
                        continue
                    conditions: list[str] = []
                    if table_name == "raw_daily_bars":
                        conditions.append("adjust = 'qfq'")
                    conditions.append(date_clause)
                    where_sql = f"WHERE {' AND '.join(conditions)}"
                    select_fields = self._select_field_expressions(table_name, table_fields)
                    sql = (
                        f"SELECT symbol, trade_date, {', '.join(select_fields)} "
                        f"FROM {table_name} {where_sql}"
                    )
                    try:
                        df = conn.execute(sql).fetchdf()
                        if not df.empty:
                            frames[table_name] = df
                    except Exception as exc:
                        structured_errors.append({
                            "source_table": table_name,
                            "required_fields": list(table_fields),
                            "category": "sql",
                            "correlation_id": _uuid.uuid4().hex[:8],
                            "message": f"{type(exc).__name__}: {exc}",
                            "sql": sql,
                        })
                        continue

                if (not financial_frame.empty or not valuation_frame.empty) and "raw_daily_bars" not in frames:
                    spine_sql = (
                        "SELECT symbol, trade_date FROM raw_daily_bars "
                        "WHERE adjust = 'qfq' AND " + date_clause
                    )
                    try:
                        spine = conn.execute(spine_sql).fetchdf()
                        if not spine.empty:
                            frames["raw_daily_bars"] = spine
                    except Exception as exc:
                        structured_errors.append({
                            "source_table": "raw_daily_bars",
                            "required_fields": ["symbol", "trade_date"],
                            "category": "sql",
                            "correlation_id": _uuid.uuid4().hex[:8],
                            "message": f"{type(exc).__name__}: {exc}",
                            "sql": spine_sql,
                        })
        except Exception as exc:
            structured_errors.append({
                "source_table": None,
                "required_fields": list(fields),
                "category": "read",
                "correlation_id": _uuid.uuid4().hex[:8],
                "message": f"warehouse connection failed: {type(exc).__name__}: {exc}",
            })
            return pd.DataFrame(), structured_errors

        if not frames:
            return pd.DataFrame(), structured_errors

        exact_frames = list(frames.values())
        result = exact_frames[0]
        for df in exact_frames[1:]:
            result = result.merge(df, on=["symbol", "trade_date"], how="outer")

        if not financial_frame.empty:
            result = self._merge_financial_asof(result, financial_frame)
        if not valuation_frame.empty:
            result = self._merge_valuation_asof(result, valuation_frame)

        result = result.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        return result, structured_errors

    # ── 面板批量执行（多交易日×多标的） ──────────────────

    def execute_panel(
        self,
        plan: ExecutionPlan,
        *,
        start_date: date,
        end_date: date,
    ) -> PanelExecutionOutcome:
        """批量计算因子面板（解决滚动窗口预热 + 横截面按天独立后处理）。

        流程：
        1. 计算预热窗口：warmup_days = max_lookback + 30
        2. 读取 [warmup_start, end_date] 全量源数据
        3. AST evaluate 在完整历史上执行（滚动函数在每个 symbol 的完整时间序列上正确运算）
        4. 横截面后处理按 trade_date 分组独立执行
        5. 截取 [start_date, end_date] 作为正式信号日
        6. 构建 MultiIndex 长表 (trade_date, symbol) × {raw, winsorized, normalized}
        """
        # 1. 计算预热窗口
        max_window = getattr(plan, "max_lookback", None)
        if max_window is None:
            max_window = plan.data_dependencies.get("max_lookback", 1)
        warmup_days = max(int(max_window), 1) + 30

        # 获取仓库中所有可用交易日（DESC 排序），用于向前推 warmup 个交易日
        all_trade_dates_desc: list[date] = self.warehouse.list_trade_dates()
        all_trade_dates_asc = sorted(all_trade_dates_desc)

        # 确定 warmup_start：找 start_date 之前 warmup_days 个交易日
        if all_trade_dates_asc:
            start_in_list = start_date
            if start_in_list not in all_trade_dates_asc:
                # 找第一个 >= start_date 的交易日
                candidates = [d for d in all_trade_dates_asc if d >= start_date]
                start_in_list = candidates[0] if candidates else all_trade_dates_asc[-1]
            try:
                start_idx = all_trade_dates_asc.index(start_in_list)
            except ValueError:
                start_idx = len(all_trade_dates_asc) - 1
            warmup_idx = max(0, start_idx - warmup_days)
            warmup_start = all_trade_dates_asc[warmup_idx]
        else:
            # 没有交易日信息时，退化为自然日估算（约 * 1.5）
            warmup_start = start_date - pd.Timedelta(days=int(warmup_days * 1.5))

        # 2. 读取完整历史源数据 [warmup_start, end_date]
        try:
            source_data, read_errors = self._read_source_data_panel(
                plan, start_date=warmup_start, end_date=end_date
            )
        except Exception as exc:
            return PanelExecutionOutcome(
                factors_long=pd.DataFrame(
                    columns=["trade_date", "symbol", "raw_value",
                             "winsorized_value", "normalized_value"]
                ),
                trade_dates=[],
                symbols=[],
                n_cells=0,
                read_errors=[{
                    "source_table": None,
                    "category": "read",
                    "correlation_id": _uuid.uuid4().hex[:8],
                    "message": f"{type(exc).__name__}: {exc}",
                }],
            )

        if source_data.empty:
            return PanelExecutionOutcome(
                factors_long=pd.DataFrame(
                    columns=["trade_date", "symbol", "raw_value",
                             "winsorized_value", "normalized_value"]
                ),
                trade_dates=[],
                symbols=[],
                n_cells=0,
                read_errors=read_errors,
            )

        # 确保 trade_date 是可比较的类型
        td_col = source_data["trade_date"]
        if not hasattr(td_col.iloc[0], "__lt__") if len(td_col) > 0 else False:
            pass

        # 3. AST evaluate 在完整历史上执行（含 warmup）
        try:
            raw_values = self._evaluate(plan, source_data)
        except Exception as exc:
            return PanelExecutionOutcome(
                factors_long=pd.DataFrame(
                    columns=["trade_date", "symbol", "raw_value",
                             "winsorized_value", "normalized_value"]
                ),
                trade_dates=[],
                symbols=[],
                n_cells=0,
                read_errors=read_errors + [{
                    "category": "eval",
                    "correlation_id": _uuid.uuid4().hex[:8],
                    "message": f"{type(exc).__name__}: {exc}",
                }],
            )

        # 4. 横截面后处理按天独立执行
        trade_date_series = source_data["trade_date"]
        winsorized, normalized = apply_postprocess_cross_sectional(
            raw_values, trade_date_series, plan.postprocess
        )

        # 5. 截取 [start_date, end_date] 作为正式信号日
        def _to_date(v: Any) -> date | None:
            if isinstance(v, date) and not isinstance(v, datetime):
                return v
            if isinstance(v, datetime):
                return v.date()
            if isinstance(v, pd.Timestamp):
                return v.date()
            if isinstance(v, str):
                try:
                    return datetime.strptime(v[:10], "%Y-%m-%d").date()
                except Exception:
                    return None
            return None

        source_tds = source_data["trade_date"].apply(_to_date)
        mask = (source_tds >= start_date) & (source_tds <= end_date)

        sliced_data = source_data.loc[mask].reset_index(drop=True)
        sliced_raw = raw_values.loc[mask].reset_index(drop=True)
        sliced_winsorized = winsorized.loc[mask].reset_index(drop=True)
        sliced_normalized = normalized.loc[mask].reset_index(drop=True)

        # 6. 构建长表
        factors_long = pd.DataFrame({
            "trade_date": sliced_data["trade_date"],
            "symbol": sliced_data["symbol"],
            "raw_value": sliced_raw.values,
            "winsorized_value": sliced_winsorized.values,
            "normalized_value": sliced_normalized.values,
        })

        # 规范化 trade_date 为 date 对象
        factors_long["trade_date"] = factors_long["trade_date"].apply(_to_date)

        trade_dates = sorted(
            [d for d in factors_long["trade_date"].unique().tolist() if d is not None]
        )
        symbols = sorted(factors_long["symbol"].unique().tolist())
        n_cells = len(factors_long)

        return PanelExecutionOutcome(
            factors_long=factors_long,
            trade_dates=trade_dates,
            symbols=symbols,
            n_cells=n_cells,
            read_errors=read_errors,
        )


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════


def _safe_float(value: Any) -> float | None:
    """安全转换为 float，NaN/Inf 返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


__all__ = [
    "FactorExecutor",
    "ExecutionOutcome",
    "PreviewOutcome",
    "PanelExecutionOutcome",
    "apply_postprocess",
    "apply_postprocess_cross_sectional",
]
