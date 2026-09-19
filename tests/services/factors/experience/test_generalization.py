"""T26/F1 参数泛化单测（纯函数，无 DB）。

覆盖开发文档 §3.12 示例：mean(close,20)/mean(close,60)-1
  → 模板 + 占位符 [{name,value,range}]；实例化回具体公式。
"""
from __future__ import annotations

import pytest

from app.services.factors.experience import generalization as gen


def _doc_ast(window1: int = 20, window2: int = 60) -> dict:
    """开发文档 §3.12 示例公式的序列化 AST。"""
    return {
        "type": "Expression",
        "body": {
            "type": "BinOp",
            "op": "Sub",
            "left": {
                "type": "BinOp",
                "op": "Div",
                "left": {
                    "type": "Call",
                    "func": "mean",
                    "args": [
                        {"type": "Name", "id": "close"},
                        {"type": "Constant", "value": window1},
                    ],
                    "keywords": [],
                },
                "right": {
                    "type": "Call",
                    "func": "mean",
                    "args": [
                        {"type": "Name", "id": "close"},
                        {"type": "Constant", "value": window2},
                    ],
                    "keywords": [],
                },
            },
            "right": {"type": "Constant", "value": 1},
        },
    }


class TestGeneralize:
    def test_doc_example_placeholders(self):
        template, placeholders = gen.generalize(_doc_ast())
        # 文档 §3.12：窗口 20/60 泛化；算术操作数 1（动量比较基准）保留
        assert placeholders == [
            {"name": "n1", "value": 20, "range": [10.0, 40.0]},
            {"name": "n2", "value": 60, "range": [30.0, 120.0]},
        ]
        assert "{n1}" in template and "{n2}" in template
        assert "close" in template and "mean" in template

    def test_doc_example_template_shape(self):
        template, _ph = gen.generalize(_doc_ast())
        assert template == "mean(close, {n1}) / mean(close, {n2}) - 1"

    def test_call_arg_value_lies_in_range(self):
        for value in (1, 5, 20, 60, 250):
            _t, phs = gen.generalize({
                "type": "Expression",
                "body": {
                    "type": "Call", "func": "mean",
                    "args": [
                        {"type": "Name", "id": "close"},
                        {"type": "Constant", "value": value},
                    ],
                    "keywords": [],
                },
            })
            (ph,) = phs
            lo, hi = ph["range"]
            assert lo <= ph["value"] <= hi
            assert lo >= gen._MIN_PLACEHOLDER_VALUE

    def test_arithmetic_operand_not_generalized(self):
        """结构常数（算术操作数）保留字面：a + 5 → 模板不变。"""
        template, phs = gen.generalize({
            "type": "Expression",
            "body": {
                "type": "BinOp", "op": "Add",
                "left": {"type": "Name", "id": "close"},
                "right": {"type": "Constant", "value": 5},
            },
        })
        assert phs == []
        assert template == "close + 5"

    def test_string_constant_not_generalized(self):
        _t, phs = gen.generalize({
            "type": "Expression",
            "body": {
                "type": "Call", "func": "mean",
                "args": [
                    {"type": "Name", "id": "close"},
                    {"type": "Constant", "value": "close"},
                ],
                "keywords": [],
            },
        })
        assert phs == []

    def test_bool_constant_not_generalized(self):
        _t, phs = gen.generalize({
            "type": "Expression",
            "body": {
                "type": "Call", "func": "if_then",
                "args": [
                    {"type": "Name", "id": "close"},
                    {"type": "Constant", "value": True},
                ],
                "keywords": [],
            },
        })
        assert phs == []

    def test_additive_child_gets_parens_under_mult(self):
        template, _ph = gen.generalize({
            "type": "Expression",
            "body": {
                "type": "BinOp", "op": "Mult",
                "left": {
                    "type": "BinOp", "op": "Add",
                    "left": {"type": "Name", "id": "open"},
                    "right": {"type": "Name", "id": "close"},
                },
                "right": {"type": "Constant", "value": 2},
            },
        })
        assert template == "(open + close) * 2"

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError):
            gen.generalize("mean(close,20)")  # type: ignore[arg-type]

    def test_rejects_node_without_type(self):
        with pytest.raises(ValueError):
            gen.generalize({"body": {}})


class TestInstantiate:
    def test_doc_example_roundtrip(self):
        template, _ph = gen.generalize(_doc_ast())
        chosen = [
            {"name": "n1", "value": 35},
            {"name": "n2", "value": 90},
        ]
        formula = gen.instantiate(template, chosen)
        assert "35" in formula and "90" in formula
        assert "- 1" in formula  # 结构常数保留
        assert "{n" not in formula

    def test_missing_value_keeps_placeholder(self):
        formula = gen.instantiate("mean(close, {n1})", [{"name": "n1"}])
        assert formula == "mean(close, {n1})"

    def test_unknown_name_ignored(self):
        formula = gen.instantiate("mean(close, {n1})", [{"name": "n9", "value": 1}])
        assert formula == "mean(close, {n1})"


class TestCollectFields:
    def test_excludes_operator_names(self):
        fields = gen.collect_fields(_doc_ast())
        assert fields == ["close"]  # mean 是算子不是字段

    def test_dedup_preserves_order(self):
        ast = {
            "type": "Expression",
            "body": {
                "type": "BinOp", "op": "Add",
                "left": {"type": "Name", "id": "volume"},
                "right": {"type": "Name", "id": "close"},
            },
        }
        extra = {
            "type": "Expression",
            "body": {"type": "Name", "id": "volume"},
        }
        assert gen.collect_fields(ast) == ["volume", "close"]
        assert gen.collect_fields(extra) == ["volume"]
