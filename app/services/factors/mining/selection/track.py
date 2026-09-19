# -*- coding: utf-8 -*-
"""A1 赛道竞争（Niche）—— 设计文档 §7.7 / 向导详设 §6.5.2。

职责：决定「在哪个因子类型池里选」。6 类赛道固定（首期不再细分），
动态配额 = 保底 60% + 动态 40%，配额上限为赛道存活个体数（不足回流），
弱赛道（连续 W 代最优个体处于最末层且 ICIR 低于门槛）保底减半转出。

本模块只做配额与均衡度指标；赛道内锦标赛见 tournament.py，
跨赛道交叉率等繁殖参数属 C1/C3（单向信号流，不越权）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from app.services.factors.mining.contracts import CATEGORY_PRIORITY

#: 保底名额占需选父代总数的比例（§6.5.2(3)：保底 60% + 动态 40%）
BASE_QUOTA_RATIO = 0.6

#: 弱赛道判定：连续 W 代最优个体处于最末层且 ICIR 低于预筛门槛
WEAK_CATEGORY_GENERATIONS = 3

#: 6 条赛道（沿用契约中的特征专属度优先级顺序；trend 兜底最宽泛）
TRACKS: tuple[str, ...] = CATEGORY_PRIORITY


@dataclass
class TrackQuotaState:
    """跨代赛道状态（调用方逐代持有并回传，无隐式全局）。"""

    #: 各赛道「连续弱代数」计数（只在活跃赛道上累计，达标恢复即清零）
    weak_streaks: dict[str, int] = field(default_factory=dict)


def active_tracks(counts_by_track: Mapping[str, int]) -> list[str]:
    """活跃赛道 = 有存活个体的赛道集合（保持 TRACKS 顺序；空赛道直接关闭）。"""
    return [t for t in TRACKS if int(counts_by_track.get(t, 0)) > 0]


def update_weak_streaks(
    state: TrackQuotaState,
    *,
    best_rank_by_track: Mapping[str, int],
    best_icir_by_track: Mapping[str, float],
    population_max_rank: int,
    prefilter_icir: float,
    weak_generations: int = WEAK_CATEGORY_GENERATIONS,
) -> frozenset[str]:
    """逐代更新弱赛道计数，返回当前判定为弱的赛道集合。

    弱判定（§6.5.2(3)）：某活跃赛道其最优个体 rank 处于全种群最末层，
    且 ICIR 低于预筛门槛 → streak+1；任一条件不满足（恢复达标）→ 清零。
    """
    weak: set[str] = set()
    for track, best_rank in best_rank_by_track.items():
        is_weak_generation = (
            int(best_rank) >= int(population_max_rank)
            and float(best_icir_by_track.get(track, float("-inf")))
            < float(prefilter_icir)
        )
        if is_weak_generation:
            state.weak_streaks[track] = state.weak_streaks.get(track, 0) + 1
        else:
            state.weak_streaks.pop(track, None)
        if state.weak_streaks.get(track, 0) >= int(weak_generations):
            weak.add(track)
    return frozenset(weak)


def compute_quotas(
    total_select: int,
    counts_by_track: Mapping[str, int],
    *,
    best_rank_by_track: Mapping[str, int],
    best_icir_by_track: Mapping[str, float],
    weak_tracks: frozenset[str] | set[str] = frozenset(),
    base_ratio: float = BASE_QUOTA_RATIO,
) -> dict[str, int]:
    """动态配额算法（保底 + 动态，规避「保护落后」缺陷，§6.5.2(3)）。

    Args:
        total_select: 本代需选父代总数 S（=种群 × 选择比例）。
        counts_by_track: 各赛道存活个体数（空赛道=用户未选对应字段，不开）。
        best_rank_by_track / best_icir_by_track: 各赛道最优个体的 rank / ICIR。
        weak_tracks: 本代判定为弱的赛道（保底减半转出）。
        base_ratio: 保底比例（默认 0.6）。

    Returns:
        {赛道: 配额}（只含活跃赛道；Σ配额 == S）。

    Raises:
        ValueError: S 超过存活个体总数 / 无活跃赛道但 S>0。
    """
    total_select = int(total_select)
    if total_select < 0:
        raise ValueError("total_select 不能为负。")
    tracks = active_tracks(counts_by_track)
    total_alive = sum(int(counts_by_track[t]) for t in tracks)
    if total_select > total_alive:
        raise ValueError(
            f"需选父代总数 {total_select} 超过存活个体总数 {total_alive}。")
    if not tracks:
        if total_select > 0:
            raise ValueError("无活跃赛道（无存活个体）无法选择父代。")
        return {}

    quotas: dict[str, int] = {}

    # ── 1. 保底部分：base = floor(S × 0.6 / |C|)；弱赛道减半 ──
    base = int(math.floor(total_select * float(base_ratio) / len(tracks)))
    for track in tracks:
        share = base // 2 if track in weak_tracks else base
        quotas[track] = min(share, int(counts_by_track[track]))

    # ── 2. 动态部分：剩余名额按赛道最优个体质量加权（rank 优先、ICIR 次之）──
    def _order_key(track: str) -> tuple[float, float]:
        rank = best_rank_by_track.get(track)
        icir = best_icir_by_track.get(track)
        return (
            float(rank) if rank is not None else float("inf"),
            -float(icir) if icir is not None else float("inf"),
        )

    remaining = total_select - sum(quotas.values())
    while remaining > 0:
        open_tracks = [t for t in tracks if quotas[t] < int(counts_by_track[t])]
        if not open_tracks:
            break  # 理论不可达（S ≤ Σ存活）；防御退出
        order = sorted(open_tracks, key=_order_key)
        weights = [len(open_tracks) - pos for pos in range(len(order))]
        total_weight = sum(weights)
        allocated = 0
        for track, weight in zip(order, weights):
            room = int(counts_by_track[track]) - quotas[track]
            give = min(int(remaining * weight / total_weight), room)
            if give > 0:
                quotas[track] += give
                allocated += give
        # 余数按位次逐个 +1（rank 最优赛道优先）
        leftover = remaining - allocated
        for track in order:
            if leftover <= 0:
                break
            room = int(counts_by_track[track]) - quotas[track]
            if room > 0:
                quotas[track] += 1
                allocated += 1
                leftover -= 1
        if allocated == 0:
            break  # 防御：本轮无进展（理论不可达）
        remaining = total_select - sum(quotas.values())

    return quotas


def category_evenness(
    counts_by_track: Mapping[str, int], population_size: int,
) -> float:
    """类型均衡度（§6.5.2(5)）：(1−HHI)/(1−1/n)，取值 0~1。

    p[c] = 赛道 c 个体数 / 种群数（分母为全种群，不只活跃赛道）；
    6 类完全均匀 = 1，全是一类（或无个体）= 0。
    写入 generations 表复用为多样性曲线（D3 反馈输入）。
    """
    population_size = int(population_size)
    if population_size <= 0:
        return 0.0
    tracks = active_tracks(counts_by_track)
    n = len(tracks)
    if n <= 1:
        return 0.0
    hhi = sum(
        (float(counts_by_track[t]) / population_size) ** 2 for t in tracks)
    value = (1.0 - hhi) / (1.0 - 1.0 / n)
    return max(0.0, min(1.0, value))


__all__ = [
    "BASE_QUOTA_RATIO", "WEAK_CATEGORY_GENERATIONS", "TRACKS",
    "TrackQuotaState", "active_tracks", "update_weak_streaks",
    "compute_quotas", "category_evenness",
]
