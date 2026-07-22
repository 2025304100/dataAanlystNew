"""自定义指标公式 AST 安全沙箱（WP-P.5）。

约束（参照 project_memory 硬约束 #1）：
- ast.Pow 计算结果 > 1e100 返回 None（防 OOM）
- 只允许算术运算：+ - * / ** %
- 允许已注册函数：abs / min / max / round / sum / len / log / sqrt / exp
- 禁止：import / lambda / 函数定义 / 类定义 / 属性访问（除预定义命名空间外）

本模块用于高级筛选阶段（discovery_fast_scan._advanced_filter），
对 Top 300 标的的 K 线数据做向量化自定义指标计算。
"""
from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable

# ast.Pow 计算结果上限（防 OOM）
POW_RESULT_LIMIT = 1e100

# 指数预检查阈值（防止 9**9**9 类多级指数攻击）
_POW_EXPONENT_LIMIT = 1000

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
    "IndicatorFormulaEvaluator",
]
