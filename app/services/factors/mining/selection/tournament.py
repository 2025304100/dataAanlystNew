# -*- coding: utf-8 -*-
"""B2 锦标赛选择（Tournament）—— 设计文档 §7.7 / 向导详设 §6.5.4。

职责：决定「用什么动作把父代选出来」。赛道内循环执行：随机抽 K 个
（默认 K=3，可调 2~7）→ 用 B1 比较钥匙（rank 升 → CD 降）取最优出线 →
重复至该赛道配额满。随机性只作用在「抽谁进组」，组内比较严格按 B1 钥匙；
固定随机种子可完全复现（需求 §6.1.1 第 15 条验收）。

口径：
- **允许同一被重复抽中**（有放回抽组，K 大偏收敛的极限即全局取 Top）；
- **精英不参与锦标赛**——精英由调用方直接复制进下一代，
  传入本模块的候选列表应已排除精英；
- K 控制选择压力：K 小偏探索、K 大偏收敛，K=3 为经验平衡值。
"""
from __future__ import annotations
from typing import Any, Callable, Sequence

DEFAULT_TOURNAMENT_K = 3
K_MIN = 2
K_MAX = 7

#: 比较钥匙：item → (rank, cd)；rank 升序、同 rank 比 CD 降序
TournamentKey = Callable[[Any], tuple[int, float]]


def validate_tournament_k(k: int) -> int:
    """校验锦标赛 K 值（2~7，默认 3；越界直接拒绝，不静默钳制）。"""
    value = int(k)
    if not (K_MIN <= value <= K_MAX):
        raise ValueError(
            f"tournament_k={k} 超出允许范围 [{K_MIN}, {K_MAX}]（默认 3）。")
    return value


def _is_better(a_key: tuple[int, float], b_key: tuple[int, float]) -> bool:
    """组内比较：rank 升序优先；同 rank 比 CD 降序（CD=+inf 最优）。"""
    if a_key[0] != b_key[0]:
        return a_key[0] < b_key[0]
    return a_key[1] > b_key[1]


def tournament_select(
    candidates: Sequence[Any],
    quota: int,
    *,
    k: int = DEFAULT_TOURNAMENT_K,
    rng: Any,
    key: TournamentKey,
) -> list[Any]:
    """赛道内锦标赛选择，返回 quota 个出线个体（元素可能重复出现）。

    Args:
        candidates: 赛道内候选（**应已排除精英**）。
        quota: 该赛道需出线名额（来自 A1 compute_quotas）。
        k: 每轮抽组大小（2~7，默认 3）。
        rng: `random.Random` 实例（固定种子可复现）。
        key: 比较钥匙 item → (rank, cd)。

    Raises:
        ValueError: k 越界 / quota>0 但候选为空 / quota 为负。
    """
    quota = int(quota)
    if quota < 0:
        raise ValueError("quota 不能为负。")
    if quota == 0:
        return []
    if not candidates:
        raise ValueError("候选列表为空，无法执行锦标赛选择。")
    k = validate_tournament_k(k)

    winners: list[Any] = []
    for _ in range(quota):
        group = [candidates[rng.randrange(len(candidates))]
                 for _ in range(k)]
        best = group[0]
        best_key = key(best)
        for challenger in group[1:]:
            c_key = key(challenger)
            if _is_better(c_key, best_key):
                best, best_key = challenger, c_key
        winners.append(best)
    return winners


__all__ = [
    "DEFAULT_TOURNAMENT_K", "K_MIN", "K_MAX",
    "TournamentKey", "validate_tournament_k", "tournament_select",
]
