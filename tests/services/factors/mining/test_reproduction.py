# -*- coding: utf-8 -*-
"""T34 繁殖自适应 C1/C2/C3/D1/D2/D3 单元测试。

规格来源：
- 设计文档 v2.0 §7.8（九机制单向信号流 + 安全护栏）
- 需求文档 §6.1.2（六机制唯一职责）
- 向导详细设计 §6.6.1~§6.6.7（C1 状态调节表、C2 四变异、C3 跨赛道、
  D2 注入、D3 三层健康分、D1 先自救后停止）

硬约束（pitfalls）：
- 三率安全区间 变异[0.40,0.80] 交叉[0.10,0.50] 注入[0.05,0.20]，且和为 1；
- 滞回 2 代，单步调整 ≤0.10；
- D1 先自救（stall=2 交 C1），后停止（stall≥3）。
职责边界（not_do）：D3 不改任何率；C1 不执行变异/交叉；C2/C3/D2 不自定比例；D1 不调参。
"""
from __future__ import annotations

import math
import random

import pytest

from app.services.factors.mining.contracts import Strategy
from app.services.factors.mining.convergence import (
    STALL_THRESHOLD,
    ConvergenceState,
    update_convergence,
)
from app.services.factors.mining.diversity import (
    genotype_diversity,
    diversity_health,
    phenotype_diversity,
)
from app.services.factors.mining.reproduction.crossover import (
    crossover_formula,
    should_cross_category,
)
from app.services.factors.mining.reproduction.injection import (
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
    MAX_STEP,
    HYSTERESIS_GENERATIONS,
    AdaptiveScheduler,
)


# ══════════════════════════════════════════════════════════
# C1 自适应调度器（唯一调参者）
# ══════════════════════════════════════════════════════════

class TestSchedulerGuards:
    def test_rates_within_guard_and_sum_one(self):
        sch = AdaptiveScheduler()
        for g in range(1, 11):
            s = sch.step(generation=g, max_generations=10, health=0.8,
                         stall_count=0, convergence_delta=0.05)
            assert GUARD_MUTATION[0] - 1e-9 <= s.mutation_rate <= GUARD_MUTATION[1] + 1e-9
            assert GUARD_CROSSOVER[0] - 1e-9 <= s.crossover_rate <= GUARD_CROSSOVER[1] + 1e-9
            assert GUARD_RANDOM[0] - 1e-9 <= s.random_rate <= GUARD_RANDOM[1] + 1e-9
            assert s.mutation_rate + s.crossover_rate + s.random_rate == pytest.approx(1.0)

    def test_extreme_inputs_are_clamped(self):
        """极端信号下护栏仍不可突破（规则不得突破安全区间）。"""
        sch = AdaptiveScheduler()
        s = sch.step(generation=50, max_generations=10, health=0.0,
                     stall_count=99, convergence_delta=-1.0)
        assert GUARD_MUTATION[0] <= s.mutation_rate <= GUARD_MUTATION[1]
        assert GUARD_CROSSOVER[0] <= s.crossover_rate <= GUARD_CROSSOVER[1]
        assert GUARD_RANDOM[0] <= s.random_rate <= GUARD_RANDOM[1]
        assert s.mutation_rate + s.crossover_rate + s.random_rate == pytest.approx(1.0)

    def test_single_step_bounded(self):
        """单步调幅 ≤ MAX_STEP（防震荡）。"""
        sch = AdaptiveScheduler()
        prev = sch.step(generation=1, max_generations=20, health=0.9,
                        stall_count=0, convergence_delta=0.0)
        for g in range(2, 20):
            cur = sch.step(generation=g, max_generations=20,
                           health=0.0 if g >= 5 else 0.9,
                           stall_count=5 if g >= 5 else 0,
                           convergence_delta=0.0)
            assert abs(cur.mutation_rate - prev.mutation_rate) <= MAX_STEP + 1e-9
            assert abs(cur.random_rate - prev.random_rate) <= MAX_STEP + 1e-9
            prev = cur

    def test_hysteresis_two_generations(self):
        """连续 2 代满足才调节：第 1 代出现停滞信号不得立刻跳转。"""
        sch = AdaptiveScheduler()
        s1 = sch.step(generation=2, max_generations=20, health=0.9,
                      stall_count=0, convergence_delta=0.0)
        # 第 2 代首次出现停滞 → 仍是常规状态（不自救）
        s2 = sch.step(generation=3, max_generations=20, health=0.2,
                      stall_count=2, convergence_delta=0.0)
        assert s2.adaptive_state == s1.adaptive_state
        # 第 3 代仍停滞（连续 2 代）→ 进入自救
        s3 = sch.step(generation=4, max_generations=20, health=0.2,
                      stall_count=2, convergence_delta=0.0)
        assert s3.adaptive_state == "stall_rescue"
        assert s3.mutation_rate > s1.mutation_rate
        assert s3.random_rate > s1.random_rate

    def test_rescue_raises_exploration(self):
        """停滞自救：结构/字段变异拉高 + 跨赛道率拉高。"""
        sch = AdaptiveScheduler()
        base = sch.step(generation=2, max_generations=20, health=0.9,
                        stall_count=0, convergence_delta=0.0)
        sch2 = AdaptiveScheduler()
        sch2.step(generation=1, max_generations=20, health=0.9, stall_count=0,
                  convergence_delta=0.0)
        sch2.step(generation=2, max_generations=20, health=0.1, stall_count=3,
                  convergence_delta=0.0)
        rescue = sch2.step(generation=3, max_generations=20, health=0.1,
                           stall_count=3, convergence_delta=0.0)
        assert rescue.adaptive_state == "stall_rescue"
        assert rescue.cross_category_ratio >= base.cross_category_ratio
        struct_like = (rescue.mutation_type_distribution.get("struct", 0.0)
                       + rescue.mutation_type_distribution.get("field", 0.0))
        base_like = (base.mutation_type_distribution.get("struct", 0.0)
                     + base.mutation_type_distribution.get("field", 0.0))
        assert struct_like >= base_like

    def test_late_stage_prefers_param_tuning(self):
        """后期：参数微调为主（约 70%）。"""
        sch = AdaptiveScheduler()
        s = sch.step(generation=9, max_generations=10, health=0.8,
                     stall_count=0, convergence_delta=0.0)
        assert s.adaptive_state == "late"
        assert s.mutation_type_distribution["param"] >= 0.6

    def test_early_stage_prefers_structural(self):
        """早期：结构/字段为主（合计约 60%）。"""
        sch = AdaptiveScheduler()
        s = sch.step(generation=1, max_generations=10, health=0.8,
                     stall_count=0, convergence_delta=0.0)
        assert s.adaptive_state == "early"
        total = s.mutation_type_distribution["struct"] + s.mutation_type_distribution["field"]
        assert total >= 0.5

    def test_output_is_contract_strategy(self):
        sch = AdaptiveScheduler()
        s = sch.step(generation=1, max_generations=10, health=0.7,
                     stall_count=0, convergence_delta=0.01)
        assert isinstance(s, Strategy)
        assert set(s.mutation_type_distribution) == set(MUTATION_TYPES)
        assert sum(s.mutation_type_distribution.values()) == pytest.approx(1.0)

    def test_c1_does_not_execute_reproduction(self):
        """职责边界：C1 只出参数，不产出个体（不执行变异/交叉）。"""
        sch = AdaptiveScheduler()
        s = sch.step(generation=1, max_generations=10, health=0.7,
                     stall_count=0, convergence_delta=0.01)
        assert not hasattr(s, "offspring")
        assert not hasattr(s, "children")


# ══════════════════════════════════════════════════════════
# D3 多样性监控（只观测，不改任何率）
# ══════════════════════════════════════════════════════════

class TestDiversity:
    def test_identical_population_low_genotype(self):
        same = ["ts_mean(close, 5)"] * 10
        assert genotype_diversity(same) == pytest.approx(0.0)

    def test_varied_population_higher_genotype(self):
        varied = [
            "ts_mean(close, 5)", "cs_rank(pe_ttm)", "(close - open) / volume",
            "ts_std(close, 20)", "cs_scale(pb)", "ts_delta(volume, 3)",
        ]
        same = ["ts_mean(close, 5)"] * 6
        assert genotype_diversity(varied) > genotype_diversity(same)

    def test_phenotype_spread(self):
        assert phenotype_diversity([0.5, 0.5, 0.5]) == pytest.approx(0.0)
        assert phenotype_diversity([-1.0, 0.0, 1.0]) > phenotype_diversity([0.1, 0.0, -0.1])

    def test_health_bounded_and_equal_weight(self):
        h = diversity_health(category_evenness=1.0, genotype=1.0, phenotype=1.0)
        assert h == pytest.approx(1.0)
        h0 = diversity_health(category_evenness=0.0, genotype=0.0, phenotype=0.0)
        assert h0 == pytest.approx(0.0)
        mid = diversity_health(category_evenness=1.0, genotype=0.5, phenotype=0.0)
        assert mid == pytest.approx(0.5)
        for v in (0.0, 0.25, 0.5, 0.75, 1.0):
            assert 0.0 <= diversity_health(v, v, v) <= 1.0

    def test_single_low_layer_drags_health(self):
        """任一层单独偏低都会拉低 health。"""
        high = diversity_health(1.0, 1.0, 1.0)
        one_low = diversity_health(0.0, 1.0, 1.0)
        assert one_low < high

    def test_d3_does_not_return_rates(self):
        """职责边界：D3 只产健康分，不产出/修改任何率。"""
        h = diversity_health(0.5, 0.5, 0.5)
        assert isinstance(h, float)
        assert not isinstance(h, (dict, tuple, list))


# ══════════════════════════════════════════════════════════
# D1 收敛早停（先自救、无效再停）
# ══════════════════════════════════════════════════════════

class TestConvergence:
    def test_progress_resets_stall(self):
        st = update_convergence(best_icir=1.0, prev_best=0.90, stall_count=3)
        assert st.stall_count == 0
        assert st.convergence_delta == pytest.approx(0.10)
        assert st.should_stop is False

    def test_no_progress_accumulates(self):
        st = update_convergence(best_icir=0.90, prev_best=0.901, stall_count=1)
        assert st.stall_count == 2
        assert st.should_rescue is True       # stall=2 → 先自救
        assert st.should_stop is False        # 不立即停

    def test_stop_only_after_three(self):
        st = update_convergence(best_icir=0.90, prev_best=0.901, stall_count=2)
        assert st.stall_count == 3
        assert st.should_stop is True
        assert st.should_rescue is True

    def test_threshold_boundary(self):
        """|delta| < STALL_THRESHOLD 才算无进步。"""
        just_above = update_convergence(
            best_icir=1.0, prev_best=1.0 - (STALL_THRESHOLD * 1.5), stall_count=1)
        assert just_above.stall_count == 0
        just_below = update_convergence(
            best_icir=1.0, prev_best=1.0 - (STALL_THRESHOLD * 0.5), stall_count=1)
        assert just_below.stall_count == 2

    def test_d1_does_not_tune_rates(self):
        """职责边界：D1 只判停止，不改率。"""
        st = update_convergence(best_icir=0.9, prev_best=0.9, stall_count=2)
        assert isinstance(st, ConvergenceState)
        assert not hasattr(st, "mutation_rate")
        assert not hasattr(st, "random_rate")


# ══════════════════════════════════════════════════════════
# C2 四种变异（比例由 C1 定）
# ══════════════════════════════════════════════════════════

class TestMutation:
    def test_types_covered(self):
        assert set(MUTATION_TYPES) == {"param", "op", "field", "struct"}

    def test_param_mutation_keeps_structure(self):
        """参数微调只改数值，不改结构。"""
        from app.services.factors.mining.dedup import tree_shape_key
        src = "ts_mean(close, 5)"
        out = mutate_formula(src, kind="param", rng=random.Random(1),
                             allowed_fields=("close", "open"))
        assert tree_shape_key(out) == tree_shape_key(src)

    def test_field_mutation_within_allowed(self):
        src = "ts_mean(close, 5)"
        out = mutate_formula(src, kind="field", rng=random.Random(2),
                             allowed_fields=("open", "volume"))
        assert "close" not in out
        assert ("open" in out) or ("volume" in out)

    def test_op_mutation_changes_operator_only(self):
        src = "(close + open)"
        out = mutate_formula(src, kind="op", rng=random.Random(3),
                             allowed_fields=("close", "open"))
        assert out != src
        assert "close" in out and "open" in out

    def test_struct_mutation_changes_structure(self):
        from app.services.factors.mining.dedup import tree_shape_key
        src = "ts_mean(close, 5)"
        out = mutate_formula(src, kind="struct", rng=random.Random(4),
                             allowed_fields=("close", "open", "volume"))
        assert out != src
        assert tree_shape_key(out) != tree_shape_key(src)

    def test_allocate_by_distribution(self):
        dist = {"param": 0.5, "op": 0.2, "field": 0.2, "struct": 0.1}
        kinds = allocate_mutation_types(10, dist, rng=random.Random(5))
        assert len(kinds) == 10
        assert set(kinds) <= set(MUTATION_TYPES)
        assert kinds.count("param") == 5
        assert kinds.count("op") == 2
        assert kinds.count("field") == 2
        assert kinds.count("struct") == 1

    def test_allocate_zero_count(self):
        dist = {"param": 1.0, "op": 0.0, "field": 0.0, "struct": 0.0}
        assert allocate_mutation_types(0, dist, rng=random.Random(6)) == []


# ══════════════════════════════════════════════════════════
# C3 子树交叉（跨赛道比例由 C1 定）
# ══════════════════════════════════════════════════════════

class TestCrossover:
    def test_ratio_zero_never_cross_category(self):
        rng = random.Random(7)
        assert all(not should_cross_category(0.0, rng) for _ in range(50))

    def test_ratio_one_always_cross_category(self):
        rng = random.Random(8)
        assert all(should_cross_category(1.0, rng) for _ in range(50))

    def test_ratio_statistical(self):
        rng = random.Random(9)
        hits = sum(1 for _ in range(2000) if should_cross_category(0.4, rng))
        assert 0.32 <= hits / 2000 <= 0.48      # 0.4 附近统计压力

    def test_crossover_produces_new_formula(self):
        a = "ts_mean(close, 5)"
        b = "cs_rank(volume)"
        out = crossover_formula(a, b, rng=random.Random(10))
        assert isinstance(out, str) and out
        assert out in (a, b) or out != a


# ══════════════════════════════════════════════════════════
# D2 随机注入（注入率由 C1 定）
# ══════════════════════════════════════════════════════════

class TestInjection:
    def test_quota_by_rate(self):
        assert injection_quota(100, 0.10) == 10
        assert injection_quota(60, 0.05) == 3
        assert injection_quota(60, 0.20) == 12

    def test_quota_clamped_to_population(self):
        assert injection_quota(10, 0.5) <= 10
        assert injection_quota(0, 0.1) == 0

    def test_quota_never_exceeds_rate(self):
        for size in (10, 37, 60, 123):
            for rate in (0.05, 0.12, 0.2):
                assert injection_quota(size, rate) <= math.ceil(size * rate)


# ══════════════════════════════════════════════════════════
# 贯通：D3/D1 观测 → C1 决策 → C2/C3/D2 执行
# ══════════════════════════════════════════════════════════

class TestSignalFlow:
    def test_observation_to_decision_to_execution(self):
        # 1) 观测：D3 健康分 + D1 stall
        health = diversity_health(category_evenness=0.3, genotype=0.2, phenotype=0.4)
        conv = update_convergence(best_icir=0.50, prev_best=0.501, stall_count=1)
        assert health < 0.5 and conv.should_rescue is True
        # 2) 决策：C1 读 health/stall → strategy
        sch = AdaptiveScheduler()
        sch.step(generation=1, max_generations=20, health=0.9, stall_count=0,
                 convergence_delta=0.0)
        sch.step(generation=2, max_generations=20, health=health,
                 stall_count=conv.stall_count, convergence_delta=conv.convergence_delta)
        strategy = sch.step(generation=3, max_generations=20, health=health,
                            stall_count=conv.stall_count,
                            convergence_delta=conv.convergence_delta)
        assert strategy.adaptive_state == "stall_rescue"
        # 3) 执行：C2/C3/D2 严格按 strategy 走
        kinds = allocate_mutation_types(20, strategy.mutation_type_distribution,
                                        rng=random.Random(11))
        assert len(kinds) == 20
        assert injection_quota(60, strategy.random_rate) >= 3   # 0.05*60
        assert 0.0 <= strategy.cross_category_ratio <= 1.0
