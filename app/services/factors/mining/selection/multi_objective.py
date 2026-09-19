# -*- coding: utf-8 -*-
"""B1 多目标评价（NSGA-II 核心算法）—— 设计文档 §7.7 / 向导详设 §6.5.3。

职责：目标向量化（min 取负统一「越大越好」）→ 非支配排序得 rank →
同 rank 内算拥挤度 CD → 提供比较钥匙（rank 升序、同 rank 比 CD 降序）。

边界口径（规格强约束）：
- 启用目标数限 3~5，目标池固定不可自建（方向锁死）；
- 每个目标上的两端极值个体 CD = +inf（保住前沿极值）；
- 与已有因子的相关性**不进**目标池/帕累托，作为硬门禁在管线外层单独卡；
- 偏好档位不进入进化计算（只作用于结果端，不在本模块）；
- 缺失/NaN 目标值按 -inf（统一方向后最差）参与比较，不抛异常。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: 目标池（系统内置固定，用户只能开关，不能自建；§6.5.3(1)）
OBJECTIVE_POOL: dict[str, str] = {
    "icir": "max",
    "coverage": "max",
    "turnover": "min",
    "complexity": "min",
    "oos_stability": "max",
    "monotonicity": "max",
}

#: 默认启用目标（ICIR↑/覆盖率↑/换手率↓/复杂度↓）
DEFAULT_ENABLED_OBJECTIVES: tuple[str, ...] = (
    "icir", "coverage", "turnover", "complexity",
)

MIN_OBJECTIVES = 3
MAX_OBJECTIVES = 5


@dataclass(frozen=True)
class ObjectiveSpec:
    """单个目标的名称与方向（direction: "max" | "min"）。"""

    name: str
    direction: str

    @property
    def sign(self) -> float:
        """minim 化目标取 -1（向量化后统一「越大越好」）。"""
        return -1.0 if self.direction == "min" else 1.0


@dataclass(frozen=True)
class IndividualRank:
    """一个个体的选择钥匙（B1 输出）。"""

    index: int
    rank: int          # 1 起；rank=1 为非支配前沿
    cd: float          # 拥挤度；前沿边界个体 = +inf


def resolve_specs(enabled: Iterable[str]) -> tuple[ObjectiveSpec, ...]:
    """把启用目标名解析为规格序列；校验池内、数量 3~5、无重复。

    Raises:
        ValueError: 未知目标名 / 数量越界 / 重复启用。
    """
    names = list(enabled)
    seen: set[str] = set()
    for name in names:
        if name not in OBJECTIVE_POOL:
            raise ValueError(f"未知目标 {name!r}（目标池固定不可自建）。")
        if name in seen:
            raise ValueError(f"目标 {name!r} 重复启用。")
        seen.add(name)
    if not (MIN_OBJECTIVES <= len(names) <= MAX_OBJECTIVES):
        raise ValueError(
            f"启用目标数限 {MIN_OBJECTIVES}~{MAX_OBJECTIVES} 个，"
            f"当前 {len(names)} 个。")
    return tuple(ObjectiveSpec(name=n, direction=OBJECTIVE_POOL[n])
                 for n in names)


def normalize_objectives(
    values: Mapping[str, Any] | None, specs: Sequence[ObjectiveSpec],
) -> dict[str, float]:
    """目标向量化：min 方向取负；缺失/None/NaN → -inf（统一方向后最差）。"""
    out: dict[str, float] = {}
    for spec in specs:
        raw = values.get(spec.name) if values else None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        if value is None or math.isnan(value):
            # 缺失/NaN = 无预测力：统一方向后最差（不乘 sign，避免被翻成最优）
            out[spec.name] = float("-inf")
        else:
            out[spec.name] = spec.sign * value
    return out


def _normalized_population(
    pop: Sequence[Mapping[str, Any]], specs: Sequence[ObjectiveSpec],
) -> list[dict[str, float]]:
    return [normalize_objectives(item, specs) for item in pop]


def _dominates_vec(
    a: Mapping[str, float], b: Mapping[str, float],
) -> bool:
    """已归一（统一「越大越好」）向量间的支配判断（内部用，不做二次归一）。"""
    better_somewhere = False
    for name, x in a.items():
        y = b[name]
        if x < y:
            return False
        if x > y:
            better_somewhere = True
    return better_somewhere


def dominates(
    a: Mapping[str, Any], b: Mapping[str, Any],
    specs: Sequence[ObjectiveSpec],
) -> bool:
    """A 支配 B ⇔ A 在所有目标上不差于 B、且至少一个目标严格更好。

    只看逐维大小：不需要权重、不需要加权总分、不要求量纲一致（§6.5.3(2)）。
    """
    return _dominates_vec(
        normalize_objectives(a, specs), normalize_objectives(b, specs))


def nondominated_sort(
    pop: Sequence[Mapping[str, Any]], specs: Sequence[ObjectiveSpec],
) -> list[list[int]]:
    """非支配排序：返回索引分层（fronts[0] 即 rank=1）。

    朴素 O(m·N²) 实现——N=100、m=4 约 4 万次比较/代，毫秒级（§6.5.3(5)）。
    """
    normalized = _normalized_population(pop, specs)
    remaining = set(range(len(pop)))
    fronts: list[list[int]] = []
    while remaining:
        front = [
            i for i in remaining
            if not any(
                _dominates_vec(normalized[j], normalized[i])
                for j in remaining if j != i
            )
        ]
        if not front:  # 防御（NaN/inf 互配不应出现全空层）
            front = [min(remaining)]
        fronts.append(front)
        remaining -= set(front)
    return fronts


def crowding_distance(
    pop: Sequence[Mapping[str, Any]],
    front: Sequence[int],
    specs: Sequence[ObjectiveSpec],
) -> dict[int, float]:
    """同 rank 前沿内算拥挤度 CD（§6.5.3(4)）。

    每个目标排序后取前后邻居间距除以该前沿极差（归一化消量纲），
    m 个目标累加；**每个目标上的两端极值个体 CD=+inf**。
    极差为 0 或非有限时该维贡献 0（不产生除零/NaN）。
    """
    normalized = _normalized_population(pop, specs)
    cd: dict[int, float] = {i: 0.0 for i in front}
    if len(front) <= 2:
        return {i: float("inf") for i in front}
    for spec in specs:
        order = sorted(front, key=lambda i: normalized[i][spec.name])
        cd[order[0]] = float("inf")
        cd[order[-1]] = float("inf")
        lo = normalized[order[0]][spec.name]
        hi = normalized[order[-1]][spec.name]
        span = hi - lo
        if not math.isfinite(span) or span <= 0.0:
            continue
        for prev_i, cur_i, next_i in zip(order, order[1:], order[2:]):
            if cd[cur_i] == float("inf"):
                continue
            cur = normalized[cur_i][spec.name]
            left = normalized[prev_i][spec.name]
            right = normalized[next_i][spec.name]
            if not (math.isfinite(cur) and math.isfinite(left)
                    and math.isfinite(right)):
                continue
            cd[cur_i] += (right - left) / span
    return cd


def rank_population(
    pop: Sequence[Mapping[str, Any]], specs: Sequence[ObjectiveSpec],
) -> list[IndividualRank]:
    """全种群 rank + CD，返回按比较钥匙排序的结果（rank 升、同 rank CD 降）。

    排序稳定：同 rank 同 CD 时保持输入序（可复现）。
    """
    fronts = nondominated_sort(pop, specs)
    ranks: dict[int, int] = {}
    cds: dict[int, float] = {}
    for level, front in enumerate(fronts, start=1):
        front_cd = crowding_distance(pop, front, specs)
        for i in front:
            ranks[i] = level
            cds[i] = front_cd[i]
    ranked = [IndividualRank(index=i, rank=ranks[i], cd=cds[i])
              for i in range(len(pop))]
    ranked.sort(key=lambda r: (r.rank, -r.cd))
    return ranked


def better_than(a: IndividualRank, b: IndividualRank) -> bool:
    """比较算子（字典序，质量优先，§6.5.3(5)）：

    A 优于 B ⇔ rank_A < rank_B，或 rank_A == rank_B 且 CD_A > CD_B。
    档次不同时拥挤度不起作用——永远不为多样性选更差的因子。
    """
    if a.rank != b.rank:
        return a.rank < b.rank
    return a.cd > b.cd


def compare_key(rank: int, cd: float) -> tuple[int, float]:
    """升序排序键（rank 升、CD 降）；CD=+inf 排最前。"""
    return (rank, -cd)


__all__ = [
    "OBJECTIVE_POOL", "DEFAULT_ENABLED_OBJECTIVES",
    "MIN_OBJECTIVES", "MAX_OBJECTIVES",
    "ObjectiveSpec", "IndividualRank",
    "resolve_specs", "normalize_objectives", "dominates",
    "nondominated_sort", "crowding_distance", "rank_population",
    "better_than", "compare_key",
]
