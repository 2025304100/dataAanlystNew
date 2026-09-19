"""F1 三层指纹（开发文档 §3.12：语义类别 + 统计指纹 + 结构哈希）。

目的：把「同构不同参数」的公式归并为同一条经验（去重）：
    mean(close,20)/mean(close,60)-1
    mean(close,10)/mean(close,30)-1   ← 同一指纹
而结构不同的公式必然不同指纹。

三层构成（全部确定性，禁 Python 内置 hash —— 有随机化种子，§3.13）：
  L1 结构哈希  —— 数值常量归一为 "N" 后 canonical JSON 的 sha256
  L2 统计指纹  —— 算子多重集 + 字段多重集 的 canonical JSON sha256
  L3 语义桶    —— AST 深度 / 算子数 / 字段数 的粗粒度桶
最终 fingerprint = sha256(L1 ‖ L2 ‖ L3) hex（64 位，落库 String(64) 唯一索引）。
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.core.hash_utils import canonical_json


def _normalize_constants(node: dict[str, Any]) -> dict[str, Any]:
    """数值常量 → {"type": "Constant", "value": "N"}（结构对齐）。"""
    out = dict(node)
    ntype = out.get("type")
    if ntype == "Expression" and "body" in out:
        out["body"] = _normalize_constants(out["body"])
    elif ntype == "BinOp":
        out["left"] = _normalize_constants(out["left"])
        out["right"] = _normalize_constants(out["right"])
    elif ntype == "UnaryOp":
        out["operand"] = _normalize_constants(out["operand"])
    elif ntype == "Call":
        out["args"] = [_normalize_constants(a) for a in out.get("args", [])]
        out["keywords"] = [
            {"arg": kw.get("arg"), "value": _normalize_constants(kw["value"])}
            for kw in out.get("keywords", [])
        ]
    elif ntype == "Constant" and isinstance(out.get("value"), (int, float)):
        out["value"] = "N"
    return out


def _collect_ops_and_fields(
    node: dict[str, Any], ops: list[str], fields: list[str]
) -> None:
    """收集算子名（Call.func）与字段名（Name，func 位置已压成字符串不算）。"""
    ntype = node.get("type")
    if ntype == "Expression":
        _collect_ops_and_fields(node["body"], ops, fields)
    elif ntype == "BinOp":
        _collect_ops_and_fields(node["left"], ops, fields)
        _collect_ops_and_fields(node["right"], ops, fields)
    elif ntype == "UnaryOp":
        _collect_ops_and_fields(node["operand"], ops, fields)
    elif ntype == "Call":
        func = str(node.get("func", ""))
        if func and func != "<unknown>":
            ops.append(func)
        for a in node.get("args", []):
            _collect_ops_and_fields(a, ops, fields)
        for kw in node.get("keywords", []):
            _collect_ops_and_fields(kw["value"], ops, fields)
    elif ntype == "Name":
        fields.append(str(node["id"]))


def _depth(node: dict[str, Any]) -> int:
    ntype = node.get("type")
    if ntype == "Expression":
        return _depth(node["body"])
    if ntype in ("BinOp",):
        return 1 + max(_depth(node["left"]), _depth(node["right"]))
    if ntype == "UnaryOp":
        return 1 + _depth(node["operand"])
    if ntype == "Call":
        child_depths = [_depth(a) for a in node.get("args", [])]
        child_depths += [_depth(kw["value"]) for kw in node.get("keywords", [])]
        return 1 + (max(child_depths) if child_depths else 0)
    return 0


def _sha(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def three_layer_fingerprint(formula_ast: dict[str, Any]) -> str:
    """三层指纹（64 hex）：结构哈希 + 统计指纹 + 语义桶 的合成摘要。"""
    if not isinstance(formula_ast, dict):
        raise ValueError("formula_ast 必须是序列化 AST dict")

    # L1 结构哈希：数值归一后的 canonical JSON
    l1 = _sha(_normalize_constants(formula_ast))

    # L2 统计指纹：算子多重集 + 字段多重集（排序保确定）
    ops: list[str] = []
    fields: list[str] = []
    _collect_ops_and_fields(formula_ast, ops, fields)
    l2 = _sha({"ops": sorted(ops), "fields": sorted(fields)})

    # L3 语义桶：深度 / 算子数 / 字段数
    l3 = _sha({
        "depth": _depth(formula_ast),
        "operators": len(ops),
        "fields": len(fields),
    })

    return hashlib.sha256((l1 + l2 + l3).encode("utf-8")).hexdigest()


def complexity_profile(formula_ast: dict[str, Any]) -> dict[str, int]:
    """复杂度画像：{"operators": 不同算子数, "nesting_depth": AST 深度,
    "field_refs": 不同字段数}（落 complexity_json 列）。"""
    ops: list[str] = []
    fields: list[str] = []
    _collect_ops_and_fields(formula_ast, ops, fields)
    return {
        "operators": len(set(ops)),
        "nesting_depth": _depth(formula_ast),
        "field_refs": len(set(fields)),
    }


def infer_category(formula_ast: dict[str, Any]) -> str:
    """自动分类（启发式，确定性；payload 显式传 category 时优先）。

    规则（按字段/算子特征，首条命中即返回）：
      - 含估值字段（pe/pb/ps/market_cap/dividend_yield…）      → valuation
      - 含基本面字段（roe/eps/revenue/profit/margin/turnover…）→ quality
      - 顶层/高频算子为波动类（std/atr/volatility）            → volatility
      - 其余（量价字段为主）                                    → volume_price
    trend/reversal 依赖方向语义，AST 无法可靠判定，不自动给出——
    需要这两类时由调用方显式指定。
    """
    ops: list[str] = []
    fields: list[str] = []
    _collect_ops_and_fields(formula_ast, ops, fields)
    field_text = " ".join(fields).lower()

    valuation_tokens = ("pe", "pb", "ps", "market_cap", "dividend_yield",
                        "total_mv", "circ_mv")
    quality_tokens = ("roe", "roa", "eps", "revenue", "profit", "margin",
                      "asset_turnover", "gross_margin", "debt_ratio",
                      "netcashflow", "operating_cashflow")
    if any(t in field_text for t in valuation_tokens):
        return "valuation"
    if any(t in field_text for t in quality_tokens):
        return "quality"
    if any(op_name in ("std", "atr", "volatility", "rolling_std") for op_name in ops):
        return "volatility"
    return "volume_price"


__all__ = [
    "three_layer_fingerprint",
    "complexity_profile",
    "infer_category",
]
