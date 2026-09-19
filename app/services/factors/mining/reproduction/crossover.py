# -*- coding: utf-8 -*-
"""C3 子树交叉（向导 §6.6.4）——**执行器，不自定跨赛道比例**。

- 是否跨赛道由 C1 的 `cross_category_ratio` 决定（`should_cross_category`）；
- 两个父代**各剪一个子树交换拼接**；后代 category 由调用方按
  `category.classify` 重新判定（§6.5.2 归类规则，不在此处硬编码）；
- 跨赛道交叉须通过字段依赖/复杂度/去重校验，不合法由调用方丢弃补位。

**职责红线（not_do）**：C3 不自定跨赛道比例。
"""
from __future__ import annotations

import math
import random
import re

_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(")


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def should_cross_category(ratio: float, rng: random.Random) -> bool:
    """按 C1 给的跨赛道比例决定本次是否跨赛道抽父代。"""
    return rng.random() < _clamp01(ratio)


def _call_spans(formula: str) -> list[tuple[int, int, str]]:
    """提取所有 `name(args)` 调用片段（含嵌套），作为可交换子树。"""
    spans: list[tuple[int, int, str]] = []
    for m in _CALL_RE.finditer(formula):
        start = m.start(1)
        i = m.end() - 1          # 指向 '('
        depth = 0
        for j in range(i, len(formula)):
            if formula[j] == "(":
                depth += 1
            elif formula[j] == ")":
                depth -= 1
                if depth == 0:
                    spans.append((start, j + 1, formula[start:j + 1]))
                    break
    return spans


def crossover_formula(a: str, b: str, *, rng: random.Random) -> str:
    """子树交叉：从 a 剪一个子树、从 b 剪一个子树，交换后拼接。

    任一侧无可交换子树（纯字段/常量）→ 退化为二元组合，保证仍产出新个体。
    """
    src_a = str(a or "").strip()
    src_b = str(b or "").strip()
    if not src_a and not src_b:
        return ""
    if not src_a:
        return src_b
    if not src_b:
        return src_a

    spans_a = _call_spans(src_a)
    spans_b = _call_spans(src_b)
    if not spans_a or not spans_b:
        op = rng.choice(("+", "-", "*"))
        return f"({src_a} {op} {src_b})"

    sa, ea, sub_a = rng.choice(spans_a)
    _sb, _eb, sub_b = rng.choice(spans_b)
    child = src_a[:sa] + sub_b + src_a[ea:]
    if child == src_a:                      # 退化（替换内容相同）→ 二元兜底
        op = rng.choice(("+", "-", "*"))
        return f"({src_a} {op} {src_b})"
    return child


__all__ = ["should_cross_category", "crossover_formula"]
