# -*- coding: utf-8 -*-
"""C1 自适应调度器（向导 §6.6.3 / 设计 §7.8 / 需求 §6.1.2）——**唯一调参者**。

输入（全部来自现成信号，不新增数据）：
- `health`：D3 三层多样性健康分 ∈[0,1]
- `stall_count` / `convergence_delta`：D1 产出
- 进化阶段：`early`（前 30% 代）/ `mid` / `late`（后 30% 代）

输出：`contracts.Strategy`（本代繁殖策略，落 generations 表可复现）。

**状态调节规则（规则式，不用模型）**

| 状态 | 判定 | 变异率 | 注入率 | 变异类型倾向 | 跨赛道率 |
|------|------|--------|--------|-------------|---------|
| early | 前 30% 代 | 0.70 | 0.12 | 结构/字段为主 | 0.30 |
| mid | 正常 | 0.60 | 0.10 | 四种均衡 | 0.20 |
| late | 后 30% 代 | 0.45 | 0.05 | 参数微调为主 | 0.10 |
| stall_rescue | health<0.5 持续 2 代 或 stall≥2 | 0.75 | 0.15 | 结构/字段拉高 | 0.40 |
| fast_progress | delta 显著为正 | 0.45 | 0.05 | 参数微调为主 | 0.15 |

**安全护栏（写死，规则不得突破）**
- 三率夹在 变异[0.40,0.80] / 交叉[0.10,0.50] / 注入[0.05,0.20]，且**和为 1**；
- **防抖滞回**：异常状态（自救/快速进步）连续 2 代满足才切换；
- **单步限幅**：相对上代单步调整 ≤0.10，避免这代升下代降震荡；
- 阶段态（early/mid/late）由代数直接确定，**不属于防抖对象**。

**职责红线（not_do）**：C1 只算参数，**不执行变异/交叉/注入**，
也**不产出任何个体**（那是 C2/C3/D2 的事）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from app.services.factors.mining.contracts import Strategy

#: 三率安全区间（写死，不可被规则突破）
GUARD_MUTATION: tuple[float, float] = (0.40, 0.80)
GUARD_CROSSOVER: tuple[float, float] = (0.10, 0.50)
GUARD_RANDOM: tuple[float, float] = (0.05, 0.20)

#: 单步调整上限（防震荡）
MAX_STEP = 0.10

#: 防抖滞回代数（异常状态连续多少代满足才切换）
HYSTERESIS_GENERATIONS = 2

#: 三率和归一化容差
RATE_SUM_TOLERANCE = 1e-6

#: 健康分偏低阈值（D3 信号）
HEALTH_LOW = 0.5

#: 阶段切分比例（前 30% / 后 30%）
EARLY_RATIO = 0.3
LATE_RATIO = 0.7

#: 停滞自救触发的 stall 下限（D1 信号）
STALL_RESCUE = 2

#: 变异类型（C2 四种，顺序固定）
MUTATION_TYPES: tuple[str, ...] = ("param", "op", "field", "struct")


@dataclass(frozen=True)
class _Profile:
    """某状态下的目标参数（交叉率由 `1 - m - r` 导出，保证和为 1）。"""

    mutation: float
    random: float
    mutation_types: Mapping[str, float]
    cross_category_ratio: float


#: 状态 → 目标参数（向导 §6.6.3 调节表）
PROFILES: dict[str, _Profile] = {
    "early": _Profile(
        mutation=0.70, random=0.12,
        mutation_types={"param": 0.20, "op": 0.20, "field": 0.30, "struct": 0.30},
        cross_category_ratio=0.30),
    "mid": _Profile(
        mutation=0.60, random=0.10,
        mutation_types={"param": 0.25, "op": 0.25, "field": 0.25, "struct": 0.25},
        cross_category_ratio=0.20),
    "late": _Profile(
        mutation=0.45, random=0.05,
        mutation_types={"param": 0.70, "op": 0.10, "field": 0.10, "struct": 0.10},
        cross_category_ratio=0.10),
    "stall_rescue": _Profile(
        mutation=0.75, random=0.15,
        mutation_types={"param": 0.20, "op": 0.20, "field": 0.30, "struct": 0.30},
        cross_category_ratio=0.40),
    "fast_progress": _Profile(
        mutation=0.45, random=0.05,
        mutation_types={"param": 0.70, "op": 0.10, "field": 0.10, "struct": 0.10},
        cross_category_ratio=0.15),
}


def _clamp(value: float, low: float, high: float) -> float:
    if not math.isfinite(value):
        return low
    return min(high, max(low, float(value)))


def _step_toward(current: float, target: float, max_step: float = MAX_STEP) -> float:
    """向目标逼近，单步不超过 `max_step`（防震荡）。"""
    if not math.isfinite(target):
        return current
    diff = target - current
    if abs(diff) <= max_step:
        return target
    return current + math.copysign(max_step, diff)


def normalize_rates(
    mutation: float, crossover: float, random_: float,
) -> tuple[float, float, float]:
    """三率归一：夹进安全区间并**保证和为 1**（系统设计 §7.8 硬约束）。

    交叉率以 `1 - 变异 - 注入` 导出；若被夹到区间边界导致和偏离 1，
    则依次回补变异率、注入率（均在各自区间内），最终兜底按比例缩放。
    """
    m = _clamp(mutation, *GUARD_MUTATION)
    r = _clamp(random_, *GUARD_RANDOM)
    c = _clamp(1.0 - m - r, *GUARD_CROSSOVER)
    total = m + c + r
    if abs(total - 1.0) > RATE_SUM_TOLERANCE:
        m = _clamp(1.0 - c - r, *GUARD_MUTATION)
        total = m + c + r
    if abs(total - 1.0) > RATE_SUM_TOLERANCE:
        r = _clamp(1.0 - c - m, *GUARD_RANDOM)
        total = m + c + r
    if abs(total - 1.0) > RATE_SUM_TOLERANCE and total > 0:
        m, c, r = m / total, c / total, r / total
        m = _clamp(m, *GUARD_MUTATION)
        c = _clamp(c, *GUARD_CROSSOVER)
        r = _clamp(r, *GUARD_RANDOM)
    return m, c, r


def _normalize_distribution(dist: Mapping[str, float]) -> dict[str, float]:
    """变异类型归一：补齐四类型、负值归零、总和为 1。"""
    raw = {k: max(0.0, float(dist.get(k, 0.0))) for k in MUTATION_TYPES}
    total = sum(raw.values())
    if total <= 0:
        return {k: 0.25 for k in MUTATION_TYPES}
    return {k: v / total for k, v in raw.items()}


@dataclass
class AdaptiveScheduler:
    """C1 调度器（有状态：滞回计数 + 上代策略，用于限步）。

    每代调用 `step()` 一次；同一 scheduler **不可跨 run 复用**（状态不共享）。
    """

    #: 初始状态（第 0 代的基准，取 mid 的稳态值）
    state: str = "mid"
    _pending: str | None = field(default=None, repr=False)
    _pending_count: int = field(default=0, repr=False)
    _mutation: float = field(default=PROFILES["mid"].mutation, repr=False)
    _random: float = field(default=PROFILES["mid"].random, repr=False)
    _cross_category_ratio: float = field(
        default=PROFILES["mid"].cross_category_ratio, repr=False)

    # ── 信号 → 目标状态 ────────────────────────────────────────────
    @staticmethod
    def _stage_of(generation: int, max_generations: int) -> str:
        if max_generations <= 0:
            return "mid"
        ratio = float(generation) / float(max_generations)
        if ratio <= EARLY_RATIO:
            return "early"
        if ratio >= LATE_RATIO:
            return "late"
        return "mid"

    @staticmethod
    def _anomaly_of(
        *, health: float | None, stall_count: int, convergence_delta: float,
        threshold: float,
    ) -> str | None:
        """异常状态信号（需滞回确认）；无异常返回 None → 走阶段态。"""
        if stall_count >= STALL_RESCUE:
            return "stall_rescue"
        if health is not None and math.isfinite(health) and health < HEALTH_LOW:
            return "stall_rescue"
        if math.isfinite(convergence_delta) and convergence_delta > threshold:
            return "fast_progress"
        return None

    def step(
        self,
        *,
        generation: int,
        max_generations: int,
        health: float | None = None,
        stall_count: int = 0,
        convergence_delta: float = 0.0,
        threshold: float = 0.01,
    ) -> Strategy:
        """产出本代繁殖策略 `Strategy`（唯一调参出口）。

        阶段态直接切换（代数决定，不需防抖）；异常态需连续
        `HYSTERESIS_GENERATIONS` 代满足才切换。三率先限步、再夹区间、最后归一。
        """
        stage = self._stage_of(int(generation), int(max_generations))
        anomaly = self._anomaly_of(
            health=health, stall_count=int(stall_count),
            convergence_delta=float(convergence_delta), threshold=float(threshold))

        if anomaly is None:
            # 无异常信号：阶段态直通（不是防抖对象）
            self.state = stage
            self._pending = None
            self._pending_count = 0
        elif anomaly == self.state:
            self._pending = None
            self._pending_count = 0
        else:
            if self._pending == anomaly:
                self._pending_count += 1
            else:
                self._pending = anomaly
                self._pending_count = 1
            if self._pending_count >= HYSTERESIS_GENERATIONS:
                self.state = anomaly
                self._pending = None
                self._pending_count = 0

        profile = PROFILES.get(self.state, PROFILES["mid"])

        # 单步限幅（相对上代）→ 夹区间 → 归一（和为 1）
        self._mutation = _step_toward(self._mutation, profile.mutation)
        self._random = _step_toward(self._random, profile.random)
        m, c, r = normalize_rates(self._mutation, 1.0 - self._mutation - self._random,
                                  self._random)
        self._mutation, self._random = m, r
        self._cross_category_ratio = _step_toward(
            self._cross_category_ratio, profile.cross_category_ratio)

        return Strategy(
            mutation_rate=m,
            crossover_rate=c,
            random_rate=r,
            mutation_type_distribution=_normalize_distribution(profile.mutation_types),
            cross_category_ratio=self._cross_category_ratio,
            adaptive_state=self.state,
        )


__all__ = [
    "GUARD_MUTATION", "GUARD_CROSSOVER", "GUARD_RANDOM",
    "MAX_STEP", "HYSTERESIS_GENERATIONS", "RATE_SUM_TOLERANCE",
    "HEALTH_LOW", "STALL_RESCUE", "MUTATION_TYPES", "PROFILES",
    "AdaptiveScheduler", "normalize_rates",
]
