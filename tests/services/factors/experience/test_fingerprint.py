"""T26/F1 三层指纹单测（纯函数）。

核心契约：同构不同参数 → 同指纹（去重）；结构不同 → 必不同。
全部确定性（禁内置 hash），可重复验证。
"""
from __future__ import annotations

import pytest

from app.services.factors.experience import fingerprint as fp


def _mean_call(field: str, window: int) -> dict:
    return {
        "type": "Expression",
        "body": {
            "type": "Call", "func": "mean",
            "args": [
                {"type": "Name", "id": field},
                {"type": "Constant", "value": window},
            ],
            "keywords": [],
        },
    }


class TestThreeLayerFingerprint:
    def test_same_structure_different_params_same_fp(self):
        assert (fp.three_layer_fingerprint(_mean_call("close", 20))
                == fp.three_layer_fingerprint(_mean_call("close", 60)))

    def test_different_field_different_fp(self):
        assert (fp.three_layer_fingerprint(_mean_call("close", 20))
                != fp.three_layer_fingerprint(_mean_call("volume", 20)))

    def test_different_operator_different_fp(self):
        assert (fp.three_layer_fingerprint(_mean_call("close", 20))
                != fp.three_layer_fingerprint({
                    "type": "Expression",
                    "body": {
                        "type": "Call", "func": "std",
                        "args": [
                            {"type": "Name", "id": "close"},
                            {"type": "Constant", "value": 20},
                        ],
                        "keywords": [],
                    },
                }))

    def test_fp_is_deterministic_64hex(self):
        value = fp.three_layer_fingerprint(_mean_call("close", 20))
        assert len(value) == 64
        int(value, 16)  # 可解析为 hex
        assert value == fp.three_layer_fingerprint(_mean_call("close", 20))

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError):
            fp.three_layer_fingerprint("mean(close,20)")  # type: ignore[arg-type]


class TestComplexityProfile:
    def test_doc_example(self):
        ast = {
            "type": "Expression",
            "body": {
                "type": "BinOp", "op": "Div",
                "left": {
                    "type": "Call", "func": "mean",
                    "args": [
                        {"type": "Name", "id": "close"},
                        {"type": "Constant", "value": 20},
                    ],
                    "keywords": [],
                },
                "right": {
                    "type": "Call", "func": "mean",
                    "args": [
                        {"type": "Name", "id": "close"},
                        {"type": "Constant", "value": 60},
                    ],
                    "keywords": [],
                },
            },
        }
        assert fp.complexity_profile(ast) == {
            "operators": 1,  # mean 重复出现只计一次
            "nesting_depth": 2,  # Div → Call
            "field_refs": 1,
        }


class TestInferCategory:
    def test_valuation_by_field(self):
        assert fp.infer_category(_mean_call("pe_ttm", 20)) == "valuation"

    def test_quality_by_field(self):
        assert fp.infer_category(_mean_call("roe", 20)) == "quality"

    def test_volatility_by_operator(self):
        assert fp.infer_category({
            "type": "Expression",
            "body": {
                "type": "Call", "func": "std",
                "args": [
                    {"type": "Name", "id": "close"},
                    {"type": "Constant", "value": 20},
                ],
                "keywords": [],
            },
        }) == "volatility"

    def test_default_volume_price(self):
        assert fp.infer_category(_mean_call("close", 20)) == "volume_price"
