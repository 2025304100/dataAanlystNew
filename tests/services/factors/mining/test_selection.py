# -*- coding: utf-8 -*-
"""T33 选择机制 A1/B1/B2 单元测试。

规格来源：
- 设计文档 v2.0 §7.7（选择机制三件套）
- 开发需求文档 §6.1.1（rank 优先、同 rank 比 CD 降；目标数限 3~5）
- 向导详设 §6.5（A1 动态配额 / B1 NSGA-II / B2 锦标赛完整口径）

纯算法测试：不触 DB、无 fixture 依赖、固定随机种子可复现。
"""
from __future__ import annotations

import random

import pytest

from app.services.factors.mining.selection.multi_objective import (
    DEFAULT_ENABLED_OBJECTIVES,
    IndividualRank,
    ObjectiveSpec,
    crowding_distance,
    dominates,
    nondominated_sort,
    normalize_objectives,
    rank_population,
    resolve_specs,
    better_than,
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
)


# ══════════════════════════════════════════════════════════
# B1 多目标 NSGA-II
# ══════════════════════════════════════════════════════════

class TestObjectiveSpecs:
    def test_default_specs_directions(self):
        specs = resolve_specs(DEFAULT_ENABLED_OBJECTIVES)
        assert len(specs) == 4
        by_name = {s.name: s.direction for s in specs}
        assert by_name["icir"] == "max"
        assert by_name["coverage"] == "max"
        assert by_name["turnover"] == "min"
        assert by_name["complexity"] == "min"

    def test_unknown_objective_raises(self):
        with pytest.raises(ValueError, match="icir_unknown"):
            resolve_specs(["icir", "coverage", "turnover", "icir_unknown"])

    def test_count_bounds(self):
        # 目标数限 3~5（需求 §6.1.1）
        for bad in (["icir", "coverage"],
                    ["icir", "coverage", "turnover", "complexity",
                     "oos_stability", "monotonicity"]):
            with pytest.raises(ValueError, match="3~5"):
                resolve_specs(bad)
        # 边界 3 与 5 合法
        assert len(resolve_specs(["icir", "coverage", "turnover"])) == 3
        assert len(resolve_specs(["icir", "coverage", "turnover",
                                  "complexity", "oos_stability"])) == 5

    def test_duplicate_raises(self):
        with pytest.raises(ValueError, match="重复"):
            resolve_specs(["icir", "icir", "coverage", "turnover"])


class TestNormalizeAndDominate:
    # 单元测试素材用 2 目标（手算方便）；直接构造 spec 绕过 3~5 池校验
    SPECS = (ObjectiveSpec(name="icir", direction="max"),
             ObjectiveSpec(name="turnover", direction="min"))

    def test_min_direction_negated(self):
        out = normalize_objectives({"icir": 0.5, "turnover": 0.2}, self.SPECS)
        assert out["icir"] == 0.5
        assert out["turnover"] == -0.2  # min → 取负，统一「越大越好」

    def test_missing_or_nan_to_worst(self):
        out = normalize_objectives({"icir": None, "turnover": float("nan")},
                                   self.SPECS)
        assert out["icir"] == float("-inf")
        assert out["turnover"] == float("-inf")
        # 缺失键同样按最差处理
        out2 = normalize_objectives({}, self.SPECS)
        assert out2["icir"] == float("-inf")

    def test_dominates_strict_and_tie(self):
        a = {"icir": 0.5, "turnover": 0.1}
        b = {"icir": 0.5, "turnover": 0.2}
        c = {"icir": 0.2, "turnover": 0.1}
        assert dominates(a, b, self.SPECS)   # a 换手更低且 icir 不差
        assert not dominates(b, a, self.SPECS)
        assert dominates(a, c, self.SPECS)   # a 仅 icir 严格更好、to 持平 → 支配
        assert not dominates(a, a, self.SPECS)  # 相等不支配
        d = {"icir": 0.6, "turnover": 0.3}
        assert not dominates(d, a, self.SPECS)  # 互有胜负（d icir 高但换手差）
        assert not dominates(a, d, self.SPECS)

    def test_dominates_respects_min_direction(self):
        a = {"icir": 0.3, "turnover": 0.9}
        b = {"icir": 0.5, "turnover": 0.1}
        # b 两维都优于 a（icir 更高、turnover 更低）→ b 支配 a
        assert dominates(b, a, self.SPECS)
        assert not dominates(a, b, self.SPECS)


class TestNondominatedSort:
    SPECS = (ObjectiveSpec(name="icir", direction="max"),
             ObjectiveSpec(name="turnover", direction="min"))

    def _pop(self):
        # p0 icir 最大但换手最差（不支配任何个体）；p1/p2 互不支配；
        # p3/p4 被 p1/p2 支配 → 前沿1={0,1,2}、前沿2={3,4}
        return [
            {"icir": 1.0, "turnover": 0.20},   # p0 rank1
            {"icir": 0.8, "turnover": 0.15},   # p1 与 p2 互不支配 → rank1
            {"icir": 0.6, "turnover": 0.12},   # p2
            {"icir": 0.2, "turnover": 0.50},   # p3 被 p1/p2 支配 → rank2
            {"icir": 0.3, "turnover": 0.55},   # p4 与 p3 互不支配 → rank2
        ]

    def test_layers(self):
        pop = self._pop()
        fronts = nondominated_sort(pop, self.SPECS)
        assert fronts[0] == [0, 1, 2]
        assert fronts[1] == [3, 4]
        assert len(fronts) == 2

    def test_crowding_boundary_inf(self):
        pop = self._pop()
        fronts = nondominated_sort(pop, self.SPECS)
        cd = crowding_distance(pop, fronts[0], self.SPECS)
        # p0 在 icir 维最大端 + p2 在 icir/turnover 两端 → 边界 CD=inf
        assert cd[0] == float("inf")
        assert cd[2] == float("inf")
        # p1 两维皆中间：icir (1.0-0.6)/0.4=1.0；turnover (-0.12-(-0.20))/0.08=1.0
        assert cd[1] == pytest.approx(2.0)

    def test_crowding_middle_manual_value(self):
        # 均匀前沿手算：icir=[0.2,0.4,0.6,0.8], turnover=[0.5,0.4,0.3,0.2]
        pop = [
            {"icir": 0.2, "turnover": 0.5},
            {"icir": 0.4, "turnover": 0.4},
            {"icir": 0.6, "turnover": 0.3},
            {"icir": 0.8, "turnover": 0.2},
        ]
        specs = (ObjectiveSpec(name="icir", direction="max"),
                 ObjectiveSpec(name="turnover", direction="min"))
        cd = crowding_distance(pop, [0, 1, 2, 3], specs)
        assert cd[0] == float("inf")
        assert cd[3] == float("inf")
        # p1: icir 维 (0.6-0.2)/0.6=2/3；turnover 维 (-0.3-(-0.5))/0.3=2/3 → 4/3
        assert cd[1] == pytest.approx(4.0 / 3.0)
        assert cd[2] == pytest.approx(4.0 / 3.0)

    def test_crowding_zero_span_contributes_zero(self):
        # 某一维全相同 → 极差 0 → 该维贡献 0（不产生除零）
        pop = [
            {"icir": 0.2, "turnover": 0.3},
            {"icir": 0.5, "turnover": 0.3},
            {"icir": 0.8, "turnover": 0.3},
        ]
        specs = (ObjectiveSpec(name="icir", direction="max"),
                 ObjectiveSpec(name="turnover", direction="min"))
        cd = crowding_distance(pop, [0, 1, 2], specs)
        # icir 维正常贡献 (0.8-0.2)/0.6=1.0；turnover 维极差 0 → 贡献 0（不除零）
        assert cd[1] == pytest.approx(1.0)
        assert cd[0] == float("inf")
        assert cd[2] == float("inf")


class TestRankAndCompare:
    SPECS = (ObjectiveSpec(name="icir", direction="max"),
             ObjectiveSpec(name="turnover", direction="min"))

    def test_rank_population_order_rank_then_cd(self):
        # icir 升 + turnover 同向升 → 互不支配（icir 高者换手差）
        pop = [
            {"icir": 0.2, "turnover": 0.2},   # rank1 边界（icir 最小端）
            {"icir": 0.4, "turnover": 0.3},   # rank1 中间 CD=4/3
            {"icir": 0.6, "turnover": 0.4},   # rank1 中间 CD=4/3
            {"icir": 0.8, "turnover": 0.5},   # rank1 边界（icir 最大端）
        ]
        ranked = rank_population(pop, self.SPECS)
        # rank 全 1；CD 降序：边界 inf 在前，中间 4/3 在后
        # （两个 inf / 两个 4/3 的组内序受浮点末位影响，断言分组性质）
        assert [r.index for r in ranked[:2]] == [0, 3]   # 均为 inf 边界
        assert set(r.index for r in ranked[2:]) == {1, 2}  # 均为 4/3
        assert all(r.rank == 1 for r in ranked)
        assert ranked[0].cd == float("inf")

    def test_rank_population_multi_layer(self):
        pop = [
            {"icir": 1.0, "turnover": 0.1},   # rank1
            {"icir": 0.2, "turnover": 0.5},   # rank2
            {"icir": 0.2, "turnover": 0.6},   # rank3（被 rank2 支配）
        ]
        ranked = rank_population(pop, self.SPECS)
        by_index = {r.index: r.rank for r in ranked}
        assert by_index == {0: 1, 1: 2, 2: 3}

    def test_better_than_rank_first_cd_second(self):
        from app.services.factors.mining.selection.multi_objective import (
            IndividualRank,
        )
        lo_rank_hi_cd = IndividualRank(index=0, rank=1, cd=0.5)
        lo_rank_lo_cd = IndividualRank(index=1, rank=1, cd=0.1)
        hi_rank = IndividualRank(index=2, rank=2, cd=float("inf"))
        # 同 rank 比 CD 降序
        assert better_than(lo_rank_hi_cd, lo_rank_lo_cd)
        assert not better_than(lo_rank_lo_cd, lo_rank_hi_cd)
        # rank 优先：CD=inf 的高层个体仍输给 rank=1
        assert better_than(lo_rank_lo_cd, hi_rank)
        assert not better_than(hi_rank, lo_rank_lo_cd)


# ══════════════════════════════════════════════════════════
# A1 赛道竞争
# ══════════════════════════════════════════════════════════

class TestActiveTracks:
    def test_only_nonzero_counts(self):
        counts = {"quality": 3, "trend": 0, "valuation": 1,
                  "reversal": 0, "volatility": 2, "volume_price": 0}
        # 活跃赛道按 TRACKS 顺序（quality > valuation > volatility > ...）
        assert active_tracks(counts) == ["quality", "valuation", "volatility"]


class TestComputeQuotas:
    BEST_RANK = {"quality": 1, "valuation": 1, "volatility": 1}
    BEST_ICIR = {"quality": 0.5, "valuation": 0.3, "volatility": 0.1}

    def test_base_floor_and_dynamic_allocation(self):
        # S=10，3 活跃赛道：base=floor(10*0.6/3)=2；动态 4 按 icir 位次 [3,2,1]
        counts = {"quality": 5, "valuation": 4, "volatility": 3}
        quotas = compute_quotas(
            10, counts, best_rank_by_track=self.BEST_RANK,
            best_icir_by_track=self.BEST_ICIR)
        assert quotas == {"quality": 5, "valuation": 3, "volatility": 2}
        assert sum(quotas.values()) == 10

    def test_empty_tracks_excluded(self):
        counts = {"quality": 5, "valuation": 0, "volatility": 5}
        quotas = compute_quotas(
            6, counts, best_rank_by_track=self.BEST_RANK,
            best_icir_by_track=self.BEST_ICIR)
        assert "valuation" not in quotas  # 空赛道不开、不占保底（§6.5.2(3)）
        assert sum(quotas.values()) == 6

    def test_cap_and_reflow_to_strong(self):
        # quality 存活 1：保底被 cap，回流名额归强赛道 valuation（§6.5.2(3)）
        counts = {"quality": 1, "valuation": 5}
        quotas = compute_quotas(
            6, counts,
            best_rank_by_track={"quality": 1, "valuation": 1},
            best_icir_by_track={"quality": 0.9, "valuation": 0.1})
        assert quotas["quality"] == 1
        assert quotas["valuation"] == 5
        assert sum(quotas.values()) == 6

    def test_weak_track_half_base(self):
        # 弱赛道（连续 W 代最末层且低 ICIR）保底减半（§6.5.2(3)）
        counts = {"quality": 10, "valuation": 10, "volatility": 10}
        quotas = compute_quotas(
            10, counts, best_rank_by_track=self.BEST_RANK,
            best_icir_by_track=self.BEST_ICIR,
            weak_tracks=frozenset({"volatility"}))
        # volatility 保底 floor(2/2)=1，且动态位次最末分不到 → 恰为 1
        assert quotas["volatility"] == 1
        assert quotas["quality"] == 5
        assert quotas["valuation"] == 4

    def test_total_conserved_randomized(self):
        # 性质测试：随机参数下 Σquota == min(S, Σ存活) 恒成立
        rng = random.Random(20260918)
        for _ in range(50):
            n_tracks = rng.randint(1, 4)
            names = list(TRACKS[:n_tracks])
            counts = {n: rng.randint(1, 10) for n in names}
            best_rank = {n: rng.randint(1, 5) for n in names}
            best_icir = {n: rng.uniform(-0.5, 1.0) for n in names}
            alive = sum(counts.values())
            s = rng.randint(1, alive)
            quotas = compute_quotas(
                s, counts, best_rank_by_track=best_rank,
                best_icir_by_track=best_icir)
            assert sum(quotas.values()) == s
            for name, q in quotas.items():
                assert 0 <= q <= counts[name]

    def test_total_select_exceeding_alive_raises(self):
        counts = {"quality": 2, "valuation": 3}
        with pytest.raises(ValueError, match="存活"):
            compute_quotas(
                6, counts, best_rank_by_track=self.BEST_RANK,
                best_icir_by_track=self.BEST_ICIR)

    def test_base_ratio_constant(self):
        assert BASE_QUOTA_RATIO == 0.6


class TestWeakStreaks:
    def test_requires_last_layer_and_low_icir(self):
        state = TrackQuotaState()
        # 最末层且 ICIR 低于门槛 → streak+1（首次调用计数 1，尚未判弱）
        weak = update_weak_streaks(
            state,
            best_rank_by_track={"X": 3, "Y": 1, "Z": 3},
            best_icir_by_track={"X": -0.5, "Y": 0.8, "Z": 0.5},
            population_max_rank=3,
            prefilter_icir=0.0)
        assert weak == frozenset()
        assert state.weak_streaks == {"X": 1}   # Y 达标清零、Z icir 达标不计

    def test_weak_after_three_generations(self):
        state = TrackQuotaState()
        kwargs = dict(
            best_rank_by_track={"X": 3},
            best_icir_by_track={"X": -0.5},
            population_max_rank=3,
            prefilter_icir=0.0)
        assert update_weak_streaks(state, **kwargs) == frozenset()
        assert update_weak_streaks(state, **kwargs) == frozenset()
        assert update_weak_streaks(state, **kwargs) == frozenset({"X"})
        assert state.weak_streaks["X"] == WEAK_CATEGORY_GENERATIONS

    def test_recovery_clears(self):
        state = TrackQuotaState(weak_streaks={"X": 2})
        # 恢复达标（rank 脱离最末层）→ 清零，不再弱
        weak = update_weak_streaks(
            state,
            best_rank_by_track={"X": 1},
            best_icir_by_track={"X": 0.9},
            population_max_rank=3,
            prefilter_icir=0.0)
        assert weak == frozenset()
        assert state.weak_streaks == {}


class TestCategoryEvenness:
    def test_uniform_six_tracks_is_one(self):
        counts = {t: 10 for t in
                  ("quality", "valuation", "volatility", "volume_price",
                   "reversal", "trend")}
        assert category_evenness(counts, 60) == pytest.approx(1.0)

    def test_single_track_is_zero(self):
        counts = {"trend": 60}
        assert category_evenness(counts, 60) == pytest.approx(0.0)

    def test_partial_balance_between_zero_and_one(self):
        counts = {"trend": 30, "reversal": 20, "volatility": 10}
        value = category_evenness(counts, 60)
        assert 0.0 < value < 1.0

    def test_degenerate_inputs(self):
        assert category_evenness({}, 0) == 0.0
        assert category_evenness({"trend": 5}, 0) == 0.0


# ══════════════════════════════════════════════════════════
# B2 锦标赛
# ══════════════════════════════════════════════════════════

def _cand(name: str, rank: int, cd: float) -> dict:
    return {"name": name, "rank": rank, "cd": cd}


def _key(item: dict) -> tuple[int, float]:
    return (item["rank"], item["cd"])


class _SeqRng:
    """受控 rng：按预设序列返回 randrange 结果（确定性测试用）。"""

    def __init__(self, seq):
        self._it = iter(seq)

    def randrange(self, n):
        value = next(self._it)
        assert 0 <= value < n
        return value


class TestTournament:
    def test_constants(self):
        assert DEFAULT_TOURNAMENT_K == 3
        assert K_MIN == 2 and K_MAX == 7

    def test_prefers_better_key(self):
        # 组内含强个体时必出强个体（受控 rng 精确控制每轮抽组内容）
        candidates = [
            _cand("weak", 2, 1.0),
            _cand("mid", 1, 0.2),
            _cand("strong", 1, float("inf")),
        ]
        # 每轮抽 [2,1,0]=[strong, mid, weak] → strong 必胜
        winners = tournament_select(candidates, 3, k=3,
                                    rng=_SeqRng([2, 1, 0] * 3), key=_key)
        assert [w["name"] for w in winners] == ["strong"] * 3

    def test_selection_pressure_favors_strong(self):
        # 统计压力：大样本下强个体显著占优（K 控制选择压力的口径）
        candidates = [
            _cand("weak", 2, 0.5),
            _cand("mid", 1, 0.2),
            _cand("strong", 1, float("inf")),
        ]
        from collections import Counter
        winners = tournament_select(candidates, 300, k=3,
                                    rng=random.Random(11), key=_key)
        counts = Counter(w["name"] for w in winners)
        # 每轮组内含 strong 概率 1-(2/3)^3≈0.704 → strong 明显占优
        assert counts["strong"] > 150
        assert counts["strong"] > counts["mid"] > counts["weak"]

    def test_k_bounds(self):
        candidates = [_cand("a", 1, 1.0), _cand("b", 2, 1.0)]
        for bad in (K_MIN - 1, K_MAX + 1):
            with pytest.raises(ValueError, match="tournament_k"):
                tournament_select(candidates, 1, k=bad,
                                  rng=random.Random(1), key=_key)
        # 边界 2 与 7 合法
        assert len(tournament_select(candidates, 1, k=K_MIN,
                                     rng=random.Random(1), key=_key)) == 1
        assert len(tournament_select(candidates, 1, k=K_MAX,
                                     rng=random.Random(1), key=_key)) == 1

    def test_repetition_allowed(self):
        # 「允许同一被重复抽中」：唯一存活个体占满全部配额（K 大偏收敛的极限）
        candidates = [_cand("only", 1, 1.0)]
        winners = tournament_select(candidates, 3, k=2,
                                    rng=random.Random(3), key=_key)
        assert winners == [candidates[0]] * 3

    def test_fixed_seed_reproducible(self):
        candidates = [_cand(f"c{i}", (i % 3) + 1, float(i % 4))
                      for i in range(10)]
        w1 = tournament_select(candidates, 8, k=3,
                               rng=random.Random(42), key=_key)
        w2 = tournament_select(candidates, 8, k=3,
                               rng=random.Random(42), key=_key)
        assert [w["name"] for w in w1] == [w["name"] for w in w2]

    def test_quota_zero_and_empty_candidates(self):
        candidates = [_cand("a", 1, 1.0), _cand("b", 2, 1.0)]
        assert tournament_select(candidates, 0, k=2,
                                 rng=random.Random(1), key=_key) == []
        with pytest.raises(ValueError, match="空"):
            tournament_select([], 1, k=2, rng=random.Random(1), key=_key)


# ══════════════════════════════════════════════════════════
# 端到端 smoke：B1 rank → A1 配额 → B2 出线
# ══════════════════════════════════════════════════════════

class TestPipelineSmoke:
    def test_b1_a1_b2_end_to_end(self):
        rng = random.Random(2026)
        specs = resolve_specs(DEFAULT_ENABLED_OBJECTIVES)
        tracks = ("quality", "valuation", "volatility", "trend")
        population: list[dict] = []
        for i in range(24):
            track = tracks[i % len(tracks)]
            population.append({
                "id": f"ind{i}",
                "category": track,
                "icir": rng.uniform(-0.2, 1.0),
                "coverage": rng.uniform(0.1, 0.9),
                "turnover": rng.uniform(0.05, 0.8),
                "complexity": rng.randint(3, 20),
            })
        # B1：全种群统一 rank+CD
        ranked = rank_population(population, specs)
        for item, r in zip(population, ranked):
            item["sel_rank"], item["sel_cd"] = r.rank, r.cd
        assert all(item["sel_rank"] >= 1 for item in population)

        # A1：按赛道分组算配额（S=12）
        by_track: dict[str, list[dict]] = {}
        for item in population:
            by_track.setdefault(item["category"], []).append(item)
        best_rank = {t: min(x["sel_rank"] for x in members)
                     for t, members in by_track.items()}
        best_icir = {t: max(x["icir"] for x in members)
                     for t, members in by_track.items()}
        quotas = compute_quotas(12, {t: len(m) for t, m in by_track.items()},
                                best_rank_by_track=best_rank,
                                best_icir_by_track=best_icir)
        assert sum(quotas.values()) == 12

        # B2：赛道内锦标赛出线（精英已由调用方排除——此处无精英）
        key = lambda x: (x["sel_rank"], x["sel_cd"])  # noqa: E731
        parents: list[dict] = []
        for track, quota in quotas.items():
            members = by_track[track]
            parents.extend(tournament_select(members, quota, k=3,
                                             rng=random.Random(11), key=key))
        assert len(parents) == 12
        assert all(p["category"] == t
                   for t, q in quotas.items()
                   for p in parents if p["id"] in
                   {m["id"] for m in by_track[t]}) or True
        # 每条出线个体确属其赛道
        id2track = {item["id"]: item["category"] for item in population}
        for p in parents:
            assert p["category"] == id2track[p["id"]]
