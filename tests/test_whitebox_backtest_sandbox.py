"""白盒测试 - 回测引擎 AST 沙箱 (app.services.backtest)。

针对代码审查发现的 H-1 问题：沙箱允许 ast.Pow 可被构造为 OOM 攻击。
同时覆盖沙箱白名单、非法节点拒绝、运算异常处理等。
"""
from __future__ import annotations

import ast
import operator
import pytest

from app.services import backtest

pytestmark = pytest.mark.whitebox


def _eval(expr: str, variables=None, functions=None):
    """辅助：解析并求值表达式。"""
    tree = ast.parse(expr, mode="eval")
    # 验证节点白名单
    for node in ast.walk(tree):
        if not isinstance(node, backtest._ALLOWED_EXPR_NODES):
            return "__REJECTED__"
    return backtest._eval_sandboxed(tree, variables or {}, functions or {})


# ---------- 沙箱白名单 ----------

def test_sandbox_allows_basic_arithmetic():
    """基本四则运算应正常求值。"""
    assert _eval("1 + 2") == 3
    assert _eval("10 - 4") == 6
    assert _eval("3 * 4") == 12
    assert _eval("15 / 4") == 3.75
    assert _eval("17 % 5") == 2


def test_sandbox_allows_comparison():
    """比较运算应返回布尔值。"""
    assert _eval("3 > 2") is True
    assert _eval("2 > 3") is False
    assert _eval("5 >= 5") is True
    assert _eval("5 == 5") is True
    assert _eval("1 != 2") is True


def test_sandbox_allows_boolean_logic():
    """布尔逻辑组合应正常求值。"""
    assert _eval("True and False") is False
    assert _eval("True or False") is True
    assert _eval("not False") is True
    assert _eval("(3 > 2) and (5 > 4)") is True


def test_sandbox_rejects_attribute_access():
    """[安全] 禁止属性访问（防止 __import__、os.system 等）。"""
    tree = ast.parse("x.__class__", mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, backtest._ALLOWED_EXPR_NODES):
            assert True  # ast.Attribute 不在白名单
            return
    pytest.fail("ast.Attribute 应被白名单拒绝")


def test_sandbox_rejects_subscript():
    """[安全] 禁止下标访问（防止字典/列表注入）。"""
    tree = ast.parse("x[0]", mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, backtest._ALLOWED_EXPR_NODES):
            assert True  # ast.Subscript 不在白名单
            return
    pytest.fail("ast.Subscript 应被白名单拒绝")


def test_sandbox_rejects_lambda_and_comprehension():
    """[安全] 禁止 Lambda、列表推导等可构造闭包的节点。"""
    for src in ["lambda x: x", "[x for x in range(10)]", "{1: 2}"]:
        tree = ast.parse(src, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, backtest._ALLOWED_EXPR_NODES):
                break
        else:
            pytest.fail(f"表达式 {src} 应被白名单拒绝")


# ---------- 运算异常处理 ----------

def test_sandbox_zero_division_returns_none():
    """除零应被捕获，返回 None 而非抛异常。"""
    assert _eval("10 / 0") is None
    assert _eval("10 % 0") is None


def test_sandbox_type_error_returns_none():
    """类型不匹配运算应返回 None。"""
    assert _eval("'a' + 1", variables={}) is None


# ---------- [H-1] Pow 幂运算 DoS 风险 ----------

def test_sandbox_pow_basic():
    """正常幂运算应工作。"""
    assert _eval("2 ** 10") == 1024
    assert _eval("3 ** 3") == 27


def test_sandbox_pow_has_result_limit():
    """[H-1 已修复] ast.Pow 仍保留在白名单（兼容正常指标公式），但已替换为 _safe_pow 受限实现。

    验证：
    1) ast.Pow 在 _ALLOWED_EXPR_NODES 中（兼容正常公式如 2**10）
    2) _BIN_OPS[ast.Pow] 不再是 operator.pow，而是 _safe_pow（带结果上限）
    3) 超大结果（>1e100）返回 None
    """
    import ast as _ast
    # ast.Pow 仍在白名单（正常幂运算仍可用）
    assert _ast.Pow in backtest._ALLOWED_EXPR_NODES
    # 但已替换为受限实现（不再是 operator.pow）
    assert backtest._BIN_OPS.get(_ast.Pow) is not operator.pow, "Pow 仍是 operator.pow，H-1 漏洞未修复"
    assert backtest._BIN_OPS.get(_ast.Pow) is backtest._safe_pow
    # 超大结果返回 None（用 1e200 直接验证上限检查，不实际触发 OOM 计算）
    assert backtest._safe_pow(10, 200) is None  # 10**200 远超 1e100
    # 正常幂运算仍可用
    assert backtest._safe_pow(2, 10) == 1024
    assert backtest._safe_pow(3, 3) == 27
    # 负指数（小数）也安全
    assert backtest._safe_pow(2, -1) == 0.5


# ---------- 变量与函数沙箱 ----------

def test_sandbox_variable_lookup():
    """变量应从 variables 字典中查找。"""
    assert _eval("close", variables={"close": 10.5}) == 10.5
    assert _eval("close + open", variables={"close": 10.0, "open": 5.0}) == 15.0
    assert _eval("missing_var", variables={}) is None


def test_sandbox_function_call():
    """白名单函数应可调用。"""
    funcs = {"max": max, "min": min, "abs": abs}
    assert _eval("max(1, 2, 3)", functions=funcs) == 3
    assert _eval("min(1, 2, 3)", functions=funcs) == 1
    assert _eval("abs(-5)", functions=funcs) == 5


def test_sandbox_unknown_function_returns_none():
    """未注册的函数应返回 None。"""
    assert _eval("os.system('rm -rf')", functions={}) in (None, "__REJECTED__")


def test_sandbox_function_exception_returns_none():
    """函数内部抛异常应返回 None。"""
    funcs = {"raise_fn": lambda: 1 / 0}
    assert _eval("raise_fn()", functions=funcs) is None
