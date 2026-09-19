"""F1 参数泛化（设计文档 §6.14 / 开发文档 §3.12，模块 M14）。

职责：把公式 AST 中的**数值常量**替换为 `{n1}`/`{n2}`… 占位符，得到
参数化模板与占位符定义；抽取时按占位符取值范围实例化回具体公式。

输入 AST 形态 = `factor_compiler._serialize_ast` 的产物（稳定 JSON dict）：
    {"type": "Expression", "body": ...}
    {"type": "BinOp", "op": "Div", "left": ..., "right": ...}
    {"type": "Call", "func": "mean", "args": [...], "keywords": [...]}
    {"type": "Name", "id": "close"}
    {"type": "Constant", "value": 20}

泛化示例（开发文档 §3.12）：
    mean(close,20)/mean(close,60)-1
      → 模板  mean(close,{n1})/mean(close,{n2})-1
      → 占位符 [{"name":"n1","value":20,"range":[10,40]},
                {"name":"n2","value":60,"range":[30,120]}]

泛化规则（与文档示例严格一致）：**只泛化函数调用参数位置**（窗口类）的
数值常量；算术运算的操作数（如动量比较基准 `-1`、RSI 的 100）是结构
常数，保留字面，不产生占位符。

取值范围是**启发式**（规格未定义精确范围）：`[max(1, v//2), v*2]`，
保证 value 落在 range 内、下界 ≥1。可测试性优先：规则确定、无随机。
"""
from __future__ import annotations

from typing import Any

#: 泛化后占位符节点的类型标记（AST dict 内新增的合成节点类型）
PLACEHOLDER_TYPE = "Placeholder"

_BINOP_SYMBOL = {
    "Add": "+",
    "Sub": "-",
    "Mult": "*",
    "Div": "/",
    "Pow": "**",
    "Mod": "%",
    "FloorDiv": "//",
}

#: 占位符取值范围下界（防止 0/负窗口这类无意义实例化）
_MIN_PLACEHOLDER_VALUE = 1


def _placeholder_range(value: float) -> list[float]:
    """启发式取值范围 [max(1, v//2), v*2]（int 向下取整）。"""
    v = float(value)
    low = max(float(_MIN_PLACEHOLDER_VALUE), float(int(v) // 2))
    return [low, v * 2.0]


def _is_numeric_constant(node: dict[str, Any]) -> bool:
    """Constant 且 value 为数值（排除 bool —— bool 是 int 子类）。"""
    if node.get("type") != "Constant":
        return False
    value = node.get("value")
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _generalize_node(
    node: dict[str, Any],
    placeholders: list[dict[str, Any]],
    *,
    in_call_arg: bool = False,
) -> dict[str, Any]:
    """递归泛化：Call 参数位置的数值 Constant → Placeholder 节点。

    in_call_arg=False 的数值常量（算术操作数）保留字面——结构常数不泛化。
    占位符按出现顺序编号（n1, n2, …）。
    """
    if not isinstance(node, dict) or "type" not in node:
        raise ValueError(f"formula_ast 节点非法（缺 type）: {node!r}")

    if in_call_arg and _is_numeric_constant(node):
        name = f"n{len(placeholders) + 1}"
        value = node["value"]
        placeholders.append({
            "name": name,
            "value": value,
            "range": _placeholder_range(value),
        })
        return {"type": PLACEHOLDER_TYPE, "name": name}

    out = dict(node)
    if node.get("type") == "Expression" and "body" in node:
        out["body"] = _generalize_node(node["body"], placeholders)
    elif node.get("type") == "BinOp":
        out["left"] = _generalize_node(node["left"], placeholders)
        out["right"] = _generalize_node(node["right"], placeholders)
    elif node.get("type") == "UnaryOp":
        out["operand"] = _generalize_node(node["operand"], placeholders)
    elif node.get("type") == "Call":
        out["args"] = [
            _generalize_node(a, placeholders, in_call_arg=True)
            for a in node.get("args", [])
        ]
        out["keywords"] = [
            {
                "arg": kw.get("arg"),
                "value": _generalize_node(
                    kw["value"], placeholders, in_call_arg=True),
            }
            for kw in node.get("keywords", [])
        ]
    return out


def _render(node: dict[str, Any], parent_op: str | None = None) -> str:
    """AST dict → DSL 文本（`mean(close,{n1})` 风格；最小括号策略）。"""
    ntype = node["type"]
    if ntype == "Expression":
        return _render(node["body"])
    if ntype == PLACEHOLDER_TYPE:
        return "{" + node["name"] + "}"
    if ntype == "Name":
        return str(node["id"])
    if ntype == "Constant":
        value = node["value"]
        return repr(value) if isinstance(value, float) else str(value)
    if ntype == "BinOp":
        op_sym = _BINOP_SYMBOL.get(node.get("op", ""), node.get("op", "?"))
        # 子节点为加减运算且本层为乘除/幂时加括号（a+b)*c
        need_paren = op_sym in ("*", "/", "**", "//", "%")
        left = _render(node["left"])
        right = _render(node["right"])
        if _is_additive(node.get("left")):
            left = f"({left})"
        if _is_additive(node.get("right")):
            right = f"({right})"
        text = f"{left} {op_sym} {right}"
        if need_paren and parent_op is not None:
            text = f"({text})"
        return text
    if ntype == "UnaryOp":
        op_sym = {"USub": "-", "UAdd": "+", "Not": "not "}.get(
            node.get("op", ""), node.get("op", "?"))
        operand = _render(node["operand"])
        if node["operand"].get("type") == "BinOp":
            operand = f"({operand})"
        return f"{op_sym}{operand}"
    if ntype == "Call":
        args = [_render(a) for a in node.get("args", [])]
        kws = [
            f"{kw['arg']}={_render(kw['value'])}"
            for kw in node.get("keywords", [])
        ]
        return f"{node['func']}({', '.join(args + kws)})"
    raise ValueError(f"不支持的 AST 节点类型: {ntype}")


def _is_additive(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "BinOp" and node.get("op") in (
        "Add", "Sub")


def generalize(formula_ast: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """数值参数泛化：返回 (模板文本, 占位符定义列表)。

    占位符按出现顺序编号（n1, n2, …），每项含 name/value/range。
    纯结构节点（如 Name/字符串常量）不泛化。
    """
    if not isinstance(formula_ast, dict):
        raise ValueError("formula_ast 必须是序列化 AST dict")
    placeholders: list[dict[str, Any]] = []
    generalized = _generalize_node(formula_ast, placeholders)
    return _render(generalized), placeholders


def instantiate(template: str, placeholders: list[dict[str, Any]]) -> str:
    """按占位符取值实例化模板：`{n1}` → 具体数值。

    placeholders 项形如 {"name": "n1", "value": 35}；缺失占位符保留原样
    （显式留痕，方便上层校验），未知名字忽略。
    """
    text = template
    for ph in placeholders or []:
        name = ph.get("name")
        if name and "value" in ph:
            text = text.replace("{" + str(name) + "}", repr(ph["value"]))
    return text


def collect_fields(formula_ast: dict[str, Any]) -> list[str]:
    """收集字段引用（Name 节点，排除 Call.func 的算子名），去重保序。"""
    fields: list[str] = []

    def _walk(node: dict[str, Any], in_func_pos: bool = False) -> None:
        ntype = node.get("type")
        if ntype == "Expression":
            _walk(node["body"])
        elif ntype == "BinOp":
            _walk(node["left"])
            _walk(node["right"])
        elif ntype == "UnaryOp":
            _walk(node["operand"])
        elif ntype == "Call":
            # func 位置的名字是算子，不是字段
            _walk_dict_list(node.get("args", []))
            for kw in node.get("keywords", []):
                _walk(kw["value"])
        elif ntype == "Name":
            if not in_func_pos and node["id"] not in fields:
                fields.append(str(node["id"]))

    def _walk_dict_list(nodes: list[dict[str, Any]]) -> None:
        for n in nodes:
            _walk(n)

    _walk(formula_ast)
    return fields


__all__ = [
    "PLACEHOLDER_TYPE",
    "generalize",
    "instantiate",
    "collect_fields",
]
