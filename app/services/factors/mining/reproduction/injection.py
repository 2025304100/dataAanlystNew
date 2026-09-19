# -*- coding: utf-8 -*-
"""D2 随机注入（向导 §6.6.5）——**执行器，不自定注入率**。

- 注入数量由 C1 的 `random_rate` 决定（`injection_quota`），不再固定 10%；
- 注入个体为**受约束随机公式**（遵守已选字段范围、允许算子子集、复杂度上限），
  复用初始种群的随机生成器 `random_generator`，不另起一套生成器；
- 作用是引入变异/交叉无法产生的全新基因，防近亲繁殖；与精英保留配合，
  随机个体质量低也不会破坏当代最优。

**职责红线（not_do）**：D2 不自定注入率。
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Sequence

from app.services.factors.mining.contracts import Strategy


def injection_quota(population_size: int, random_rate: float) -> int:
    """本代注入名额 = `round(size × rate)`，夹在 [0, size]。

    `rate` 已由 C1 保证落在 [0.05, 0.20]；此处只做防御性夹取，
    不重新决策（D2 不自定注入率）。
    """
    size = max(0, int(population_size))
    if size == 0:
        return 0
    rate = float(random_rate)
    if not math.isfinite(rate) or rate <= 0:
        return 0
    rate = min(1.0, rate)
    return max(0, min(size, int(round(size * rate))))


def inject_individuals(
    *,
    strategy: Strategy,
    population_size: int,
    rng: random.Random,
    selected_fields: Sequence[str] | None = None,
    existing_formulas: Sequence[str] | None = None,
    compiler: Callable[..., Any] | None = None,
    max_operators: int | None = None,
    max_nesting: int | None = None,
) -> list[dict[str, Any]]:
    """按 C1 策略生成本代注入个体（受约束随机公式）。

    复用 `random_generator.generate_random_candidates`（与初始种群同源），
    返回候选字典列表（结构同初始种群产出）。生成器不可用时返回空列表，
    **不抛异常**——注入失败不等于进化失败，由调用方按 shortfall 处理。
    """
    quota = injection_quota(population_size, strategy.random_rate)
    if quota <= 0:
        return []

    from app.services.factors.mining.random_generator import (
        RandomGeneratorConfig, generate_random_candidates,
    )

    kwargs: dict[str, Any] = {
        "target_count": quota,
        "seed": int(rng.randrange(2 ** 32)),
    }
    if max_operators is not None:
        kwargs["max_operators"] = max_operators
    if max_nesting is not None:
        kwargs["max_nesting"] = max_nesting
    cfg = RandomGeneratorConfig(**kwargs)
    try:
        result = generate_random_candidates(
            cfg=cfg,
            selected_fields=list(selected_fields) if selected_fields else None,
            existing_formulas=list(existing_formulas) if existing_formulas else None,
            compiler=compiler,
        )
    except Exception:
        return []
    return list(getattr(result, "candidates", []) or [])


__all__ = ["injection_quota", "inject_individuals"]
