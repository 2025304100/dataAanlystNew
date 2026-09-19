# -*- coding: utf-8 -*-
"""选择机制 A1/B1/B2（M2）—— 设计文档 §7.7 / 向导详设 §6.5。

三层叠加、职责互不重叠：
- track（A1 赛道竞争）：决定「在哪个因子类型池里选」；
- multi_objective（B1 NSGA-II）：决定「用什么标准判优劣」；
- tournament（B2 锦标赛）：决定「用什么动作把父代选出来」。

总体链路（§6.5.1）：B1 全种群 rank+CD → A1 按赛道算动态配额 →
每条赛道内 B2 锦标赛出线至配额满。精英保留由调用方处理（不参与锦标赛）。
"""
from app.services.factors.mining.selection.multi_objective import (
    DEFAULT_ENABLED_OBJECTIVES,
    MAX_OBJECTIVES,
    MIN_OBJECTIVES,
    OBJECTIVE_POOL,
    IndividualRank,
    ObjectiveSpec,
    better_than,
    compare_key,
    crowding_distance,
    dominates,
    nondominated_sort,
    normalize_objectives,
    rank_population,
    resolve_specs,
)
from app.services.factors.mining.selection.track import (
    BASE_QUOTA_RATIO,
    TRACKS,
    WEAK_CATEGORY_GENERATIONS,
    TrackQuotaState,
    active_tracks,
    category_evenness,
    compute_quotas,
    update_weak_streaks,
)
from app.services.factors.mining.selection.tournament import (
    DEFAULT_TOURNAMENT_K,
    K_MAX,
    K_MIN,
    tournament_select,
    validate_tournament_k,
)

__all__ = [
    # B1
    "OBJECTIVE_POOL", "DEFAULT_ENABLED_OBJECTIVES",
    "MIN_OBJECTIVES", "MAX_OBJECTIVES",
    "ObjectiveSpec", "IndividualRank",
    "resolve_specs", "normalize_objectives", "dominates",
    "nondominated_sort", "crowding_distance", "rank_population",
    "better_than", "compare_key",
    # A1
    "TRACKS", "BASE_QUOTA_RATIO", "WEAK_CATEGORY_GENERATIONS",
    "TrackQuotaState", "active_tracks", "update_weak_streaks",
    "compute_quotas", "category_evenness",
    # B2
    "DEFAULT_TOURNAMENT_K", "K_MIN", "K_MAX",
    "validate_tournament_k", "tournament_select",
]
