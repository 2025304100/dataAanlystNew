"""自定义指标公式 AST 安全沙箱（WP-P.5）。

约束（参照 project_memory 硬约束 #1）：
- ast.Pow 计算结果 > 1e100 返回 None（防 OOM）
- 只允许算术运算：+ - * / ** %
- 允许已注册函数：abs / min / max / round / sum / len / log / sqrt / exp
- 禁止：import / lambda / 函数定义 / 类定义 / 属性访问（除预定义命名空间外）

本模块用于高级筛选阶段（discovery_fast_scan._advanced_filter），
对 Top 300 标的的 K 线数据做向量化自定义指标计算。

⚠️ 与因子 DSL（`app/services/factors/factor_compiler.py`）的关系
--------------------------------------------------------------
两者是**两套不同作用域的 DSL**，函数集**并不等价**：

    本沙箱       逐**单个标的**求值 —— context 是该标的的标量 / 1D 序列
    因子 DSL     逐**面板**求值     —— 同一交易日全截面 + 沿时间轴滚动

因此截面算子族（`cs_rank` / `cs_zscore` / …）在本沙箱中**无法有意义地求值**：
它们需要"同一交易日的全截面标的"，而本沙箱一次只看一个标的。

处理方式：`cs_*` 名仍登记进 `ALLOWED_FUNCS`（保持白名单可自省、不产生
"unsupported function" 的误导性报错），但在 `_validate()` 阶段**显式拒绝**，
给出可操作的错误信息。**刻意不做成静默返回 None** —— 那会让筛选条件悄悄失效
却不报错，属于最危险的一类失败。

⚠️ 与上面相反：`ts_*` 时序算子在本沙箱里**必须真正可用**（SD-v2.0 §12.3 D-A）
--------------------------------------------------------------------------
`ts_delta_bars(close, 20)` 在**逐标的序列**上有确切含义（末值 − 20 期前的值），
`ts_atr(close, n)` 同理。故本模块为它们提供**真实实现**（返回末期的标量值），
而不是照抄 `cs_*` 的拒绝模式。这两个算子正是需求 §6.0 说「25 个模板中 10 个不可行」
的根因，拒绝掉等于这条需求没做。

⚠️ 单标的序列**无法区分「报告期」与「交易日」**：`ts_delta_periods` 在本沙箱里
只能按位置做 N 期差分，其「报告期」语义由因子流水线（`factor_executor` + 字段
所属 `source_table`）保证。参见 `app/services/factors/dsl/time_series.py::TS_N_UNIT`。

⚠️ 本模块的实现与 `time_series.py` 是**两套**（面板 vs 单标的标量），刻意不 import
因子域模块以保持 `app/services/` 的跨域边界干净。二者数值一致性由测试
`tests/services/factors/mining/test_dsl_time_series.py` 交叉断言兜住。
"""
from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable, Sequence

# ast.Pow 计算结果上限（防 OOM）
POW_RESULT_LIMIT = 1e100

# 指数预检查阈值（防止 9**9**9 类多级指数攻击）
_POW_EXPONENT_LIMIT = 1000


def _positive_int(value: Any) -> int | None:
    """把参数规整为正整数期数；非法返回 None（由调用方转成「本函数不可求值」）。

    与 `time_series._validate_n` 口径一致：拒绝非整数（不静默截断）、拒绝 bool。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not as_float.is_integer():
        return None
    as_int = int(as_float)
    return as_int if as_int >= 1 else None


def _as_float_sequence(value: Any) -> list[float] | None:
    """把 context 里的值规整为 float 序列；不是序列（或含非数值）返回 None。"""
    if isinstance(value, (list, tuple)):
        items: Sequence[Any] = value
    elif hasattr(value, "tolist"):  # numpy.ndarray / pandas.Series
        converted = value.tolist()
        if not isinstance(converted, list):
            return None  # 标量 numpy 值 → 不接受
        items = converted
    else:
        return None
    try:
        return [float(item) for item in items]
    except (TypeError, ValueError):
        return None


def _sandbox_ts_delta(series: Any, n: Any) -> float | None:
    """单标的 N 期差分：`末值 - N 期前值`。

    面板版见 `app/services/factors/dsl/time_series.py::ts_delta_bars`。

    `ts_delta_bars` 与 `ts_delta_periods` 共用本实现 —— 在**单标的序列**上二者
    数值相同，差异仅在 `n` 的口径（交易日 / 报告期），而那是调用方与因子流水线的
    责任，沙箱无法也不应替它判断。
    """
    sequence = _as_float_sequence(series)
    if sequence is None:
        return None
    periods = _positive_int(n)
    if periods is None or len(sequence) <= periods:
        return None
    return sequence[-1] - sequence[-1 - periods]


def _sandbox_ts_atr(series: Any, n: Any) -> float | None:
    """单标的 close-to-close 平均真实波幅：最近 n 期 `|x[i] - x[i-1]|` 的均值。

    面板版见 `app/services/factors/dsl/time_series.py::ts_atr`。
    与面板版一致：**只用传入的序列**（prev_close 由序列自身位移得到），
    不依赖 high/low。
    """
    sequence = _as_float_sequence(series)
    if sequence is None:
        return None
    periods = _positive_int(n)
    if periods is None or len(sequence) < periods + 1:
        return None
    total = 0.0
    for index in range(len(sequence) - periods, len(sequence)):
        total += abs(sequence[index] - sequence[index - 1])
    return total / periods


def _cross_section_not_supported(*_args: Any, **_kwargs: Any) -> Any:
    """截面算子在单标的沙箱中的占位实现：**必须显式失败**，不得静默返回。

    正常情况下走不到这里（`_validate` 已在构造期拦截）；保留实现是为了
    万一有调用方绕过 `_validate` 直接取 `ALLOWED_FUNCS[...]` 调用时，
    仍然失败而不是产出错误数值。
    """
    raise ValueError(
        "cross-section operators require panel context (all symbols on the same "
        "trade_date) and are not available in the single-symbol indicator sandbox"
    )


#: 仅在**面板**上下文（因子流水线）中可用的截面算子。
#: 登记进 `ALLOWED_FUNCS` 以便白名单自省，但构造期即拒绝求值。
CROSS_SECTION_ONLY_FUNCS: frozenset[str] = frozenset({
    "cs_rank",
    "cs_zscore",
    "cs_demean",
    "cs_scale",
    "cs_quantile",
    "cs_winsorize",
})

#: 在**单标的**上下文中**真实可用**的时序算子（与 CROSS_SECTION_ONLY_FUNCS 相反）。
#: 前两个共用同一实现（差异只在 n 的口径），见 `_sandbox_ts_delta`。
TIME_SERIES_FUNCS: frozenset[str] = frozenset({
    "ts_delta_bars",
    "ts_delta_periods",
    "ts_atr",
})

# 允许的内置函数
ALLOWED_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sum": sum,
    "len": len,
    "log": math.log,
    "sqrt": math.sqrt,
    "exp": math.exp,
    # 截面算子族（M2 / T03）：登记但不可求值，见模块 docstring
    **{name: _cross_section_not_supported for name in sorted(CROSS_SECTION_ONLY_FUNCS)},
    # 时序算子族（M2 / T04）：**真实可用**（返回末期的标量值）
    "ts_delta_bars": _sandbox_ts_delta,
    "ts_delta_periods": _sandbox_ts_delta,
    "ts_atr": _sandbox_ts_atr,
}

# 允许的二元算术运算符
_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    # Pow 走单独的 _safe_pow
}

# 允许的一元运算符
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# 允许的 AST 节点白名单
_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)


def _safe_pow(a: Any, b: Any) -> Any:
    """受限的幂运算：防止 OOM。

    实现要点（参照 project_memory 硬约束 #1）：
    - 指数过大直接拒绝（> _POW_EXPONENT_LIMIT）
    - 预检查 log(|a|) * b，若超 log(POW_RESULT_LIMIT) 直接返回 None
    - 计算结果 > POW_RESULT_LIMIT（1e100）返回 None
    """
    if isinstance(b, int) and abs(b) > _POW_EXPONENT_LIMIT:
        return None
    try:
        a_num = float(a) if not isinstance(a, (int, float)) else a
        b_num = float(b) if not isinstance(b, (int, float)) else b
    except (TypeError, ValueError):
        return None
    # 预检查：避免实际计算时内存爆炸
    try:
        if a_num != 0:
            log_result = abs(b_num * math.log(abs(a_num)))
            if log_result > math.log(POW_RESULT_LIMIT):
                return None
        elif b_num <= 0:
            return None  # 0^负数 / 0^0 等异常
    except (ValueError, OverflowError):
        return None
    try:
        result = operator.pow(a_num, b_num)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError, MemoryError):
        return None
    if isinstance(result, (int, float)):
        if math.isnan(result) or math.isinf(result):
            return None
        if abs(result) > POW_RESULT_LIMIT:
            return None
    return result


class IndicatorFormulaEvaluator:
    """AST 安全评估器。

    用法：
        evaluator = IndicatorFormulaEvaluator("100 - (100 / (1 + rs))")
        result = evaluator.evaluate({"rs": 1.5})

    若公式含非法 AST 节点（import / lambda / 函数定义 / 属性访问等），
    构造时抛出 ValueError。
    若 ast.Pow 计算结果 > 1e100，evaluate 返回 None。
    任何评估期异常均返回 None（不向外抛）。
    """

    def __init__(self, formula: str):
        if not isinstance(formula, str) or not formula.strip():
            raise ValueError("formula must be a non-empty string")
        if len(formula) > 500:
            raise ValueError("formula is too long")
        try:
            self.tree: ast.Expression = ast.parse(formula, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"formula syntax error: {exc.msg}") from exc
        self._validate(self.tree)

    def _validate(self, node: ast.AST) -> None:
        """校验 AST 节点白名单。

        递归检查每个节点，不允许的节点类型抛 ValueError。
        特别禁止：
        - import / lambda / 函数定义 / 类定义
        - 属性访问（ast.Attribute）
        - 下标（ast.Subscript）
        - 赋值（ast.Assign / ast.AugAssign）
        - 推导式（ast.ListComp / ast.SetComp / ast.DictComp / ast.GeneratorExp）
        - lambda / awaiting / yield
        - 截面算子（CROSS_SECTION_ONLY_FUNCS）：单标的上下文无法求值，构造期即拒绝

        注意：ast.Name 节点允许任意 id，由 evaluate() 时从 context 解析；
        未在 context 中的变量返回 None，不抛异常。
        """
        for child in ast.walk(node):
            if not isinstance(child, _ALLOWED_NODES):
                raise ValueError(
                    f"unsupported AST node: {type(child).__name__}"
                )
            if isinstance(child, ast.Call):
                if not isinstance(child.func, ast.Name):
                    raise ValueError("only direct function calls are allowed")
                if child.func.id not in ALLOWED_FUNCS:
                    raise ValueError(f"unsupported function: {child.func.id}")
                if child.func.id in CROSS_SECTION_ONLY_FUNCS:
                    raise ValueError(
                        f"{child.func.id} is a cross-section operator: it needs panel "
                        "context (all symbols on the same trade_date), which the "
                        "single-symbol indicator sandbox does not provide. "
                        "Use the factor DSL / factor pipeline instead."
                    )
                if child.keywords:
                    raise ValueError("keyword arguments are not allowed")

    def evaluate(self, context: dict[str, Any]) -> float | None:
        """在给定上下文下评估公式。

        Args:
            context: 变量字典，如 {"rs": 1.5, "close": [10, 11, 12]}

        Returns:
            评估结果；ast.Pow 超限返回 None；任何异常返回 None
        """
        try:
            return self._eval_node(self.tree.body, context)
        except Exception:
            return None

    def _eval_node(self, node: ast.AST, context: dict[str, Any]) -> Any:
        if isinstance(node, ast.BinOp):
            left = self._eval_node(node.left, context)
            right = self._eval_node(node.right, context)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Pow):
                return _safe_pow(left, right)
            op_func = _BIN_OPS.get(type(node.op))
            if op_func is None:
                return None
            try:
                return op_func(left, right)
            except (TypeError, ZeroDivisionError, ValueError, OverflowError):
                return None
        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand, context)
            if operand is None:
                return None
            op_func = _UNARY_OPS.get(type(node.op))
            if op_func is None:
                return None
            try:
                return op_func(operand)
            except (TypeError, ValueError):
                return None
        if isinstance(node, ast.Call):
            args = [self._eval_node(a, context) for a in node.args]
            if any(a is None for a in args):
                return None
            func = ALLOWED_FUNCS.get(node.func.id)
            if func is None:
                return None
            try:
                return func(*args)
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                return None
        if isinstance(node, ast.Name):
            return context.get(node.id)
        if isinstance(node, ast.Constant):
            return node.value
        return None


__all__ = [
    "POW_RESULT_LIMIT",
    "ALLOWED_FUNCS",
    "CROSS_SECTION_ONLY_FUNCS",
    "TIME_SERIES_FUNCS",
    "IndicatorFormulaEvaluator",
]
