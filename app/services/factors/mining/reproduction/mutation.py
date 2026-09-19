# -*- coding: utf-8 -*-
"""C2 四种变异（向导 §6.6.4）——**执行器，不自定比例**。

四种变异（类型比例严格由 C1 的 `mutation_type_distribution` 决定）：

1. `param` 参数微调：只改数值参数（窗口/阈值），**不改结构**；
2. `op` 算子替换：同类型算子替换（`+ ↔ -`、`* ↔ /`）；
3. `field` 字段替换：换用到的字段，**必须在用户已选字段范围内**；
4. `struct` 结构变异：增删/改写子树（最激进），受复杂度上限约束。

**职责红线（not_do）**：C2 不自定比例、不定变异率——只按 C1 给的
distribution 分配数量并执行。合法性校验（AST/复杂度/字段依赖/去重）
由调用方（繁殖编排）统一处理，非法丢弃后由 D2 补位。
"""
from __future__ import annotations

import math
import random
import re

from app.services.factors.mining.reproduction.scheduler import MUTATION_TYPES

#: 字段/数字/调用识别（与 random_generator 同款口径，避免两套正则）
_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)")
_FIELD_RE = re.compile(r"(?<![A-Za-z_0-9])([a-z_][a-z_0-9]*)(?![A-Za-z_0-9(])")
_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(")

#: 同优先级算子替换对（不跨优先级，避免量纲/语义剧变）
_OP_SWAP: dict[str, tuple[str, ...]] = {
    "+": ("-", "*"),
    "-": ("+", "*"),
    "*": ("/", "-"),
    "/": ("*", "+"),
}

#: 结构变异可用的一元包裹算子
_WRAP_OPS: tuple[str, ...] = ("cs_rank", "cs_scale", "ts_std", "ts_delta", "ts_mean")

#: 参数微调的窗口范围（与 random_generator 的 WINDOW_MIN/MAX 同量级）
WINDOW_MIN = 5
WINDOW_MAX = 120


def _normalize_distribution(dist: dict[str, float] | None) -> dict[str, float]:
    raw = {k: max(0.0, float((dist or {}).get(k, 0.0))) for k in MUTATION_TYPES}
    total = sum(raw.values())
    if total <= 0:
        return {k: 0.25 for k in MUTATION_TYPES}
    return {k: v / total for k, v in raw.items()}


def allocate_mutation_types(
    count: int,
    distribution: dict[str, float] | None,
    *,
    rng: random.Random,
) -> list[str]:
    """按 C1 给的 distribution 把 `count` 个变异名额分配到四种类型。

    用**最大余数法**保证总数精确等于 `count`（不用逐次随机抽样，否则
    小样本下比例漂移明显）；余数并列时按固定类型序，保证可复现。
    最后洗牌，避免同类变异连续扎堆。
    """
    if count <= 0:
        return []
    dist = _normalize_distribution(distribution)
    raw = {k: count * dist[k] for k in MUTATION_TYPES}
    base = {k: int(math.floor(raw[k])) for k in MUTATION_TYPES}
    assigned = sum(base.values())
    remainder = max(0, int(count) - assigned)
    order = sorted(
        MUTATION_TYPES,
        key=lambda k: (-(raw[k] - base[k]), MUTATION_TYPES.index(k)))
    for i in range(remainder):
        base[order[i % len(order)]] += 1
    kinds: list[str] = []
    for k in MUTATION_TYPES:
        kinds.extend([k] * base[k])
    rng.shuffle(kinds)
    return kinds


def _mutate_param(formula: str, rng: random.Random) -> str:
    """参数微调：只改数值（窗口类参数落在 [WINDOW_MIN, WINDOW_MAX]）。"""
    matches = list(_NUM_RE.finditer(formula))
    if not matches:
        return formula
    m = rng.choice(matches)
    raw = m.group(1)
    try:
        value = float(raw)
    except ValueError:
        return formula
    is_int = "." not in raw
    candidates: list[float] = []
    for cand in (value * 2, value / 2, value + 5, max(0.0, value - 5), value + 1):
        if cand <= 0:
            continue
        if value >= 1.0 and (WINDOW_MIN <= cand <= WINDOW_MAX):
            candidates.append(cand)
        elif value < 1.0:
            candidates.append(cand)
    if not candidates:
        return formula
    new_value = rng.choice(candidates)
    text = str(int(new_value)) if is_int else f"{new_value:.4f}"
    return formula[:m.start(1)] + text + formula[m.end(1):]


def _mutate_op(formula: str, rng: random.Random) -> str:
    """算子替换：同优先级二元算子互换（`+ ↔ -`、`* ↔ /`）。"""
    for idx, ch in enumerate(formula):
        if ch in _OP_SWAP:
            target = rng.choice(_OP_SWAP[ch])
            return formula[:idx] + target + formula[idx + 1:]
    return formula


def _mutate_field(formula: str, rng: random.Random,
                  allowed_fields: tuple[str, ...] | None) -> str:
    """字段替换：**目标**字段必须落在已选范围内，源字段不受此限。

    源字段若也要求在池内，则"公式用了池外字段"时会静默不换（实测坑：
    `ts_mean(close,5)` + allowed=(open,volume) 原样返回）。变异的目的是把
    公式拉回合法字段域内，因此源字段取公式中实际用到的字段（算子已由
    `_FIELD_RE` 的后瞻 `(` 排除），目标才从 allowed 池里选。
    """
    if not allowed_fields:
        return formula
    pool = [f for f in allowed_fields if f]
    matches = list(_FIELD_RE.finditer(formula))
    if not matches:
        return formula
    m = rng.choice(matches)
    old = m.group(1)
    others = [f for f in pool if f != old]
    if not others:
        return formula
    new = rng.choice(others)
    return formula[:m.start(1)] + new + formula[m.end(1):]


def _mutate_struct(formula: str, rng: random.Random,
                   allowed_fields: tuple[str, ...] | None) -> str:
    """结构变异：包裹一层算子 / 与另一个字段做二元组合（最激进）。

    受复杂度上限约束：`max_nesting` 为 None 时不限制（由调用方校验）。
    """
    roll = rng.random()
    if roll < 0.6:
        op = rng.choice(_WRAP_OPS)
        if op.startswith("ts_") and op != "ts_delta":
            return f"{op}({formula}, {rng.randint(WINDOW_MIN, 20)})"
        return f"{op}({formula})"
    pool = [f for f in (allowed_fields or ()) if f]
    other = rng.choice(pool) if pool else "close"
    op = rng.choice(("+", "-", "*"))
    return f"({formula} {op} {other})"


def mutate_formula(
    formula: str,
    *,
    kind: str,
    rng: random.Random,
    allowed_fields: tuple[str, ...] | None = None,
) -> str:
    """按指定类型执行一次变异（类型由 C1 分配，本函数不自定比例）。

    未知类型 → 原样返回（不猜测、不抛异常，交由调用方按丢弃处理）。
    """
    src = str(formula or "")
    if not src.strip():
        return src
    if kind == "param":
        return _mutate_param(src, rng)
    if kind == "op":
        return _mutate_op(src, rng)
    if kind == "field":
        return _mutate_field(src, rng, allowed_fields)
    if kind == "struct":
        return _mutate_struct(src, rng, allowed_fields)
    return src


__all__ = [
    "MUTATION_TYPES", "WINDOW_MIN", "WINDOW_MAX",
    "allocate_mutation_types", "mutate_formula",
]
