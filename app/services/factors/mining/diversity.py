# -*- coding: utf-8 -*-
"""D3 多样性监控（向导 §6.6.6 / 设计 §7.8）——**只观测，不改任何率**。

三层多样性合成健康分 health ∈[0,1]，单向喂给 C1：

| 层级 | 指标 | 实现 |
|------|------|------|
| 类型多样性 | `category_evenness`（已在 A1/selection.track 定义） | 调用方传入 |
| 基因型多样性 | 个体两两 AST 结构**距离**均值 | `1 - structural_similarity`（复用 dedup，不重复算） |
| 表现型多样性 | 因子 ICIR 离散度 | 归一化极差 `spread/(1+spread)` |

**职责红线（not_do）**：D3 只产出 health 并写库/画图/告警，
**不修改任何变异率/注入率**——调参动作统一归 C1，避免双调度。
本模块因此**不导出任何"改率"函数**，也**不返回任何率字段**。
"""
from __future__ import annotations

import math
from typing import Sequence

from app.services.factors.mining.dedup import structural_similarity

#: 三层默认等权（高级可调权重）
DEFAULT_WEIGHTS: tuple[float, float, float] = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)

#: 基因型多样性抽样上限（种群大时两两比较 O(n²) 会拖慢每代）
GENOTYPE_SAMPLE_LIMIT = 60


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def genotype_diversity(
    formulas: Sequence[str],
    *,
    sample_limit: int = GENOTYPE_SAMPLE_LIMIT,
) -> float:
    """基因型多样性 ∈[0,1]：两两 AST 结构**距离**（`1 - similarity`）的均值。

    - 全部同构 → 0.0；越不一样越接近 1.0；
    - 样本不足（<2 个公式）→ 0.0（无从谈多样性，不虚高）；
    - 超过 `sample_limit` 时截断抽样，避免 O(n²) 拖慢进化主循环。
    """
    items = [str(f) for f in formulas if str(f).strip()]
    if len(items) < 2:
        return 0.0
    if sample_limit and len(items) > sample_limit:
        step = len(items) / float(sample_limit)
        items = [items[int(i * step)] for i in range(sample_limit)]

    total = 0.0
    pairs = 0
    for i in range(len(items) - 1):
        for j in range(i + 1, len(items)):
            sim = structural_similarity(items[i], items[j])
            total += 1.0 - _clamp01(sim)
            pairs += 1
    if pairs == 0:
        return 0.0
    return _clamp01(total / pairs)


def phenotype_diversity(icirs: Sequence[float]) -> float:
    """表现型多样性 ∈[0,1)：ICIR 离散度，用归一化极差 `spread/(1+spread)`。

    - 全部相同 → 0.0；离散越大越接近 1.0（但永不等于 1，留有余量）；
    - 用极差而非标准差：对个别离群个体不敏感，且天然落在 [0,1)；
    - 空/单样本/非有限值 → 0.0。
    """
    vals = [float(v) for v in icirs if v is not None and math.isfinite(float(v))]
    if len(vals) < 2:
        return 0.0
    spread = max(vals) - min(vals)
    if spread <= 0.0:
        return 0.0
    return _clamp01(spread / (1.0 + spread))


def diversity_health(
    category_evenness: float,
    genotype: float,
    phenotype: float,
    *,
    weights: Sequence[float] = DEFAULT_WEIGHTS,
) -> float:
    """三层合成健康分 ∈[0,1]（默认等权；任一层单独偏低都会拉低 health）。

    这是**给 C1 的唯一信号**；本函数不做任何调参动作（D3 不改率）。
    """
    layers = (_clamp01(category_evenness), _clamp01(genotype), _clamp01(phenotype))
    w = [float(x) for x in weights]
    if len(w) != 3:
        w = list(DEFAULT_WEIGHTS)
    wsum = sum(max(0.0, x) for x in w)
    if wsum <= 0.0:
        w = list(DEFAULT_WEIGHTS)
        wsum = sum(w)
    return _clamp01(sum(layer * wi for layer, wi in zip(layers, w)) / wsum)


__all__ = [
    "DEFAULT_WEIGHTS", "GENOTYPE_SAMPLE_LIMIT",
    "genotype_diversity", "phenotype_diversity", "diversity_health",
]
