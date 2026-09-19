# -*- coding: utf-8 -*-
"""繁殖与自适应（M2）：C1 调度 + C2 变异 + C3 交叉 + D2 注入。

与 `selection/`（A1/B1/B2）严格分工、信号单向流动（向导 §6.6.9）：

```
D3(diversity) / D1(convergence)  → 观测信号
    → C1(scheduler)  → Strategy（唯一调参）
    → C2(mutation) / C3(crossover) / D2(injection) → 执行
```

本包**不产出任何观测信号**（D3/D1 在 mining 包根的 diversity.py /
convergence.py），也不执行选择（A1/B1/B2 在 selection/）。
"""
from app.services.factors.mining.reproduction.crossover import (
    crossover_formula,
    should_cross_category,
)
from app.services.factors.mining.reproduction.injection import (
    inject_individuals,
    injection_quota,
)
from app.services.factors.mining.reproduction.mutation import (
    MUTATION_TYPES,
    allocate_mutation_types,
    mutate_formula,
)
from app.services.factors.mining.reproduction.scheduler import (
    GUARD_CROSSOVER,
    GUARD_MUTATION,
    GUARD_RANDOM,
    HYSTERESIS_GENERATIONS,
    MAX_STEP,
    PROFILES,
    AdaptiveScheduler,
    normalize_rates,
)

__all__ = [
    # C1 调度
    "AdaptiveScheduler", "normalize_rates", "PROFILES",
    "GUARD_MUTATION", "GUARD_CROSSOVER", "GUARD_RANDOM",
    "MAX_STEP", "HYSTERESIS_GENERATIONS",
    # C2 变异
    "MUTATION_TYPES", "allocate_mutation_types", "mutate_formula",
    # C3 交叉
    "crossover_formula", "should_cross_category",
    # D2 注入
    "injection_quota", "inject_individuals",
]
