"""P0 · M2 选择/繁殖机制接入 GA 主循环 —— 集成测试。

覆盖（对照需求 M2 验收标准 15/16 + 向导详设 §6.5/§6.6/§9.15-18）
- advanced 路径 smoke：M2 代际字段齐备、三角和守恒、种群大小稳定
- 固定种子完全可复现（需求验收第 15 条）
- 精英按 B1 钥匙保留且不参与繁殖；帕累托前沿计数 ≥1
- D1 先自救（stall≥2 喂 C1）后停止（stall≥3）：平坦 ICIR 第 4 代 converged
- adaptive=True：C1 三率始终在安全区间且和为 1；rescue 状态可达
- adaptive=False：固定三率生效、四种变异类型均分、adaptive_state="fixed"
- D2 随机注入：random_supplier 按配额被调用
- C2 变异类型分布落 record；classify_formula 重归类正确
- naive 路径回归保护：默认配置不含任何 M2 键（M1 行为零变化）
- persist_generation：M2 字段 round-trip + NaN→None
- task_runner：_resolve_selection_mode 推导 / _fill_random_exploration 随机层 /
  _make_random_supplier 供给
"""
from __future__ import annotations

import math
from datetime import datetime

import pytest

from app.models.factor_mining import FactorMiningGeneration, FactorMiningRun
from app.services.factors.mining import genetic_algorithm as GA
from app.services.factors.mining import service as SVC
from app.services.factors.mining import task_runner as TR


# ══════════════════════════════════════════════════════════
# 测试基建
# ══════════════════════════════════════════════════════════

#: 覆盖多类别的公式池（trend / volatility / valuation / volume_price / reversal）
_FORMULAS = [
    "mean(close,{n})/mean(close,{n2})-1",      # trend
    "stddev(close,{n})",                        # volatility
    "cs_rank(pe_ttm)",                          # valuation
    "cs_rank(volume)-cs_rank(mean(volume,{n}))",  # volume_price
    "-ts_delta_bars(close,{n})",                # reversal
]


def _population(n: int) -> list[dict]:
    """构造跨类别初始种群（每类都有代表，保证 A1 赛道配额可分）。"""
    out: list[dict] = []
    for i in range(n):
        tpl = _FORMULAS[i % len(_FORMULAS)]
        formula = tpl.format(n=5 + (i % 5) * 5, n2=20 + (i % 3) * 20)
        out.append({
            "formula": formula,
            "canonical_formula": formula,
            "formula_hash": f"m2-hash-{i}",
            "source": "classic", "operation": "enumerated",
            "generation": 0,
        })
    return out


def _evaluator_by_index(values: list[float]):
    """按公式哈希尾号给 ICIR（确定性，可复现场景）。"""

    def _evaluate(item):
        index = int(str(item.get("formula_hash")).rsplit("-", 1)[-1])
        return {"icir": values[index % len(values)], "coverage": 0.8,
                "turnover": 0.2, "ic_mean": 0.01, "complexity": 5}

    return _evaluate


def _flat_evaluator(value: float = 0.5):
    def _evaluate(item):
        return {"icir": value, "coverage": 0.8, "turnover": 0.2,
                "ic_mean": 0.01, "complexity": 5}
    return _evaluate


def _improving_evaluator():
    counter = {"n": 0}

    def _evaluate(item):
        counter["n"] += 1
        return {"icir": 0.1 + 0.05 * (counter["n"] // 10),
                "coverage": 0.8, "turnover": 0.2, "ic_mean": 0.01,
                "complexity": 5}
    return _evaluate


def _cfg(**kw) -> GA.GAConfig:
    base = {"population_size": 10, "max_generations": 5,
            "selection_ratio": 0.2,
            "mutation_rate": 0.55, "crossover_rate": 0.25, "random_rate": 0.20,
            "convergence_threshold": 1e-3, "convergence_generations": 2,
            "seed": 7}
    base.update(kw)
    return GA.GAConfig(**base)


@pytest.fixture
def run_row(db_session):
    row = FactorMiningRun(
        id="run-m2-1", status="running", candidate_pool_snapshot_id="snap-1",
        data_cutoff_at=datetime(2026, 9, 20), start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 9, 1), rebalance_frequency="weekly",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        max_generation=5, total_trials=0,
    )
    db_session.add(row)
    db_session.commit()
    return row


# ══════════════════════════════════════════════════════════
# 1. advanced 路径 smoke
# ══════════════════════════════════════════════════════════


class TestAdvancedSmoke:
    def test_m2_generation_fields_present_and_consistent(self):
        records: list = []
        res = GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True, max_generations=3),
            _population(10), evaluate=_improving_evaluator(),
            on_generation=lambda g, rec, ranked: records.append(dict(rec)))
        assert res.generations_run >= 1
        for rec in records:
            # M2 代际字段齐备
            assert rec["adaptive_state"] in ("early", "mid", "late",
                                             "stall_rescue", "fast_progress")
            assert rec["pareto_front_count"] >= 1
            assert 0.0 <= rec["category_evenness"] <= 1.0
            for key in ("diversity_genotype", "diversity_phenotype",
                        "diversity_health", "diversity_score"):
                assert rec[key] is not None and math.isfinite(rec[key])
            assert rec["category_distribution_json"], "类别分布必须非空"
            assert rec["cross_category_ratio"] is not None
            # 三角和守恒：分配名额之和 == 非精英名额
            slots = int(rec["population_size"]) - int(rec["elite_count"])
            assert (rec["mutation_count"] + rec["crossover_count"]
                    + rec["random_count"]) == slots
            # actual 三率 = 名额 / 非精英名额
            assert rec["actual_mutation_rate"] == pytest.approx(
                rec["mutation_count"] / slots, abs=1e-6)
            assert (rec["actual_mutation_rate"] + rec["actual_crossover_rate"]
                    + rec["actual_random_rate"]) == pytest.approx(1.0, abs=1e-6)
            # C2 变异类型分布：键 ⊆ 四种，值和为 1
            dist = rec["mutation_type_distribution_json"]
            assert set(dist) <= {"param", "op", "field", "struct"}
            if rec["mutation_count"] > 0:
                assert sum(dist.values()) == pytest.approx(1.0, abs=1e-6)

    def test_population_size_stable_advanced(self):
        sizes: list = []
        GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True,
                 population_size=12, max_generations=3),
            _population(12), evaluate=_flat_evaluator(0.5),
            on_generation=lambda g, rec, r: sizes.append(rec["population_size"]))
        assert sizes == [12, 12, 12]

    def test_elites_marked_and_not_bred_advanced(self):
        records: list = []
        GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True, max_generations=2),
            _population(10), evaluate=_flat_evaluator(0.5),
            on_generation=lambda g, rec, ranked: records.append(
                [dict(r) for r in ranked]))
        gen2_initial = records[1]
        elites = [r for r in gen2_initial if r.get("operation") == "elite"]
        assert elites, "上一代精英应原样进入下一代"
        assert all(r["operation"] not in ("mutation", "crossover", "random")
                   for r in elites)

    def test_best_icir_individual_survives_in_elites(self):
        """最高 ICIR 个体必在 rank-1 前沿 → 按 B1 钥匙保留为精英。"""
        values = [0.1, 0.9, 0.5, 0.7, 0.3, 0.2, 0.8, 0.4, 0.6, 0.05]
        records: list = []
        GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=False, max_generations=2),
            _population(10), evaluate=_evaluator_by_index(values),
            on_generation=lambda g, rec, ranked: records.append(
                [dict(r) for r in ranked]))
        first, second = records[0], records[1]
        best_gen1 = max(first, key=lambda r: (r.get("fitness") or {})
                        .get("icir", float("-inf")))
        elites_gen2 = [r for r in second if r.get("operation") == "elite"]
        assert any(e["formula_hash"] == best_gen1["formula_hash"]
                   for e in elites_gen2), "当代最优必须进入下一代精英"


# ══════════════════════════════════════════════════════════
# 2. 固定种子可复现（需求验收第 15 条）
# ══════════════════════════════════════════════════════════


class TestReproducibility:
    def test_same_seed_same_history(self):
        values = [0.1, 0.9, 0.5, 0.7, 0.3, 0.2, 0.8, 0.4, 0.6, 0.05]
        cfg = _cfg(selection_mode="advanced", adaptive=True, max_generations=4)
        res1 = GA.run_ga_loop(cfg, _population(10),
                              evaluate=_evaluator_by_index(values))
        res2 = GA.run_ga_loop(cfg, _population(10),
                              evaluate=_evaluator_by_index(values))
        assert res1.history == res2.history
        assert res1.best_formula == res2.best_formula
        assert res1.total_trials == res2.total_trials

    def test_different_seed_diverges(self):
        values = [0.1, 0.9, 0.5, 0.7, 0.3, 0.2, 0.8, 0.4, 0.6, 0.05]
        res1 = GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True, seed=7,
                 max_generations=3),
            _population(10), evaluate=_evaluator_by_index(values))
        res2 = GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True, seed=99,
                 max_generations=3),
            _population(10), evaluate=_evaluator_by_index(values))
        assert res1.history != res2.history


# ══════════════════════════════════════════════════════════
# 3. D1 收敛：先自救后停止（§6.6.7）
# ══════════════════════════════════════════════════════════


class TestConvergenceM2:
    def test_flat_icir_stops_at_stall_3_with_rescue(self):
        """平坦 ICIR：gen0 基线 → stall 1 → 2（喂 C1 自救）→ 3（停止）。"""
        res = GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True, max_generations=10),
            _population(10), evaluate=_flat_evaluator(0.5))
        assert res.stopped_reason == "converged"
        assert res.generations_run == 4, "基线代 + 连续 3 代无提升"
        assert res.stall_count == 3
        # 自救信号：最后一代 C1 状态应为 stall_rescue（滞回 2 代后切换）
        assert res.history[-1]["adaptive_state"] == "stall_rescue"
        assert res.history[-1]["stall_count"] == 3

    def test_steady_improvement_runs_all_generations(self):
        res = GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True, max_generations=4),
            _population(10), evaluate=_improving_evaluator())
        assert res.stopped_reason == "max_generations"
        assert res.generations_run == 4


# ══════════════════════════════════════════════════════════
# 4. C1 自适应护栏 / 固定策略
# ══════════════════════════════════════════════════════════


class TestSchedulerIntegration:
    def test_adaptive_rates_within_guards(self):
        """C1 三率始终夹在安全区间（§6.6.3 护栏写死，规则不得突破）。"""
        records: list = []
        GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True, max_generations=6),
            _population(10), evaluate=_improving_evaluator(),
            on_generation=lambda g, rec, r: records.append(dict(rec)))
        assert len(records) == 6
        for rec in records:
            slots = int(rec["population_size"]) - int(rec["elite_count"])
            m = rec["mutation_count"] / slots
            c = rec["crossover_count"] / slots
            r_ = rec["random_count"] / slots
            assert 0.40 - 0.13 <= m <= 0.80 + 0.13, (m, rec)
            assert 0.10 - 0.13 <= c <= 0.50 + 0.13, (c, rec)
            assert 0.05 - 0.06 <= r_ <= 0.20 + 0.13, (r_, rec)
            assert m + c + r_ == pytest.approx(1.0, abs=1e-6)

    def test_fixed_strategy_when_adaptive_off(self):
        records: list = []
        GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=False, max_generations=1),
            _population(10), evaluate=_flat_evaluator(0.5),
            on_generation=lambda g, rec, r: records.append(dict(rec)))
        rec = records[0]
        assert rec["adaptive_state"] == "fixed"
        # 固定三率：0.55/0.25/0.20 × slots 8 → 4/2/2（注入配额 round(10×0.20)=2）
        assert rec["mutation_count"] == 4
        assert rec["crossover_count"] == 2
        assert rec["random_count"] == 2
        # 四种变异类型均分（1:1:1:1）
        dist = rec["mutation_type_distribution_json"]
        assert set(dist) == {"param", "op", "field", "struct"}
        for v in dist.values():
            assert v == pytest.approx(0.25, abs=1e-6)

    def test_mutation_ops_enabled_restricts_types(self):
        records: list = []
        GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=False, max_generations=1,
                 mutation_ops_enabled=("param", "field")),
            _population(10), evaluate=_flat_evaluator(0.5),
            on_generation=lambda g, rec, r: records.append(dict(rec)))
        dist = records[0]["mutation_type_distribution_json"]
        assert set(dist) == {"param", "field"}


# ══════════════════════════════════════════════════════════
# 5. 配置校验
# ══════════════════════════════════════════════════════════


class TestConfigValidation:
    def test_invalid_selection_mode_rejected(self):
        with pytest.raises(ValueError, match="selection_mode"):
            _cfg(selection_mode="fancy")

    def test_tournament_k_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="tournament_k"):
            _cfg(selection_mode="advanced", tournament_k=8)
        with pytest.raises(ValueError, match="tournament_k"):
            _cfg(selection_mode="advanced", tournament_k=1)

    def test_invalid_objectives_rejected(self):
        with pytest.raises(ValueError, match="目标"):
            _cfg(selection_mode="advanced", objectives=("icir", "coverage"))
        with pytest.raises(ValueError, match="未知目标"):
            _cfg(selection_mode="advanced",
                 objectives=("icir", "coverage", "turnover", "nope"))

    def test_invalid_mutation_ops_rejected(self):
        with pytest.raises(ValueError, match="mutation_ops_enabled"):
            _cfg(selection_mode="advanced",
                 mutation_ops_enabled=("param", "bogus"))


# ══════════════════════════════════════════════════════════
# 6. D2 随机注入 + C2/C3 繁殖产物
# ══════════════════════════════════════════════════════════


class TestInjectionAndBreeding:
    def test_random_supplier_called_with_quota(self):
        requests: list = []

        def supplier(count):
            requests.append(int(count))
            return [{"formula": f"cs_rank(close)-{i}", "source": "random",
                     "operation": "random", "formula_hash": f"inj-{i}-{count}",
                     "category": "trend"} for i in range(count)]

        GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=False,
                 population_size=20, selection_ratio=0.2, max_generations=2),
            _population(20), evaluate=_flat_evaluator(0.5),
            random_supplier=supplier)
        assert requests and all(n >= 1 for n in requests)
        # 注入配额 = round(种群 × random_rate) = round(20 × 0.20) = 4
        assert 4 in requests

    def test_supplier_failure_falls_back_without_crash(self):
        def bad_supplier(count):
            raise RuntimeError("注入供给故障")

        res = GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=False, max_generations=2),
            _population(10), evaluate=_flat_evaluator(0.5),
            random_supplier=bad_supplier)
        assert res.generations_run >= 1
        for rec in res.history:
            slots = int(rec["population_size"]) - int(rec["elite_count"])
            assert (rec["mutation_count"] + rec["crossover_count"]
                    + rec["random_count"]) == slots

    def test_offspring_categories_reclassified(self):
        """变异/交叉后代 category 按新公式重判（§6.5.2），不沿用父代。"""
        records: list = []

        def on_generation(gen, rec, ranked):
            records.append(([dict(r) for r in ranked], dict(rec)))

        GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=False, max_generations=2),
            _population(10), evaluate=_flat_evaluator(0.5),
            on_generation=on_generation)
        # 每个个体都有类别（重归类兜底 trend）
        for ranked, _rec in records:
            for e in ranked:
                assert e.get("category") in (
                    "trend", "reversal", "volatility", "valuation",
                    "quality", "volume_price")


class TestClassifyFormula:
    """classify_formula：公式串 → 6 类（字段+算子联合判定，优先级取最高）。"""

    @pytest.mark.parametrize("formula,expected", [
        ("cs_rank(pe_ttm)", "valuation"),
        ("stddev(close,20)", "volatility"),
        ("cs_rank(volume)-cs_rank(mean(volume,20))", "volume_price"),
        ("-ts_delta_bars(close,5)", "reversal"),
        ("mean(close,20)/mean(close,60)-1", "trend"),
        ("ts_delta_periods(roe_ttm,4)", "quality"),
    ])
    def test_classification(self, formula, expected):
        assert GA.classify_formula(formula) == expected


# ══════════════════════════════════════════════════════════
# 7. naive 路径回归保护（M1 行为零变化）
# ══════════════════════════════════════════════════════════


class TestNaiveRegression:
    def test_default_config_has_no_m2_keys(self):
        records: list = []
        GA.run_ga_loop(
            _cfg(max_generations=2), _population(10),
            evaluate=_flat_evaluator(0.5),
            on_generation=lambda g, rec, r: records.append(dict(rec)))
        m2_keys = {"adaptive_state", "pareto_front_count", "category_evenness",
                   "diversity_health", "actual_mutation_rate",
                   "mutation_type_distribution_json", "convergence_delta"}
        for rec in records:
            assert not (m2_keys & set(rec)), "naive 路径不得产出 M2 键"

    def test_naive_convergence_still_uses_convergence_generations(self):
        """naive：连续 convergence_generations=2 代无提升 → 第 3 代停（M1 契约）。"""
        res = GA.run_ga_loop(
            _cfg(max_generations=10, convergence_generations=2),
            _population(10), evaluate=_flat_evaluator(0.5))
        assert res.stopped_reason == "converged"
        assert res.generations_run == 3


# ══════════════════════════════════════════════════════════
# 8. 落库：persist_generation M2 字段 round-trip
# ══════════════════════════════════════════════════════════


class TestPersistenceM2:
    def test_m2_fields_roundtrip(self, db_session, run_row):
        record = {
            "population_size": 10, "elite_count": 2,
            "mutation_count": 4, "crossover_count": 2, "random_count": 2,
            "stall_count": 1, "eliminated_count": 0,
            "adaptive_state": "stall_rescue",
            "pareto_front_count": 3,
            "category_evenness": 0.72,
            "diversity_genotype": 0.55, "diversity_phenotype": 0.31,
            "diversity_health": 0.53, "diversity_score": 0.53,
            "actual_mutation_rate": 0.5, "actual_crossover_rate": 0.25,
            "actual_random_rate": 0.25,
            "cross_category_ratio": 0.4, "convergence_delta": 0.0,
            "category_distribution_json": {"trend": 4, "volatility": 6},
            "mutation_type_distribution_json": {"param": 0.5, "struct": 0.5},
        }
        SVC.persist_generation(db_session, run_id=run_row.id, generation=0,
                                record=record)
        row = SVC.load_generation(db_session, run_id=run_row.id, generation=0)
        assert row.adaptive_state == "stall_rescue"
        assert row.pareto_front_count == 3
        assert row.category_evenness == pytest.approx(0.72)
        assert row.diversity_health == pytest.approx(0.53)
        assert row.actual_mutation_rate == pytest.approx(0.5)
        assert row.cross_category_ratio == pytest.approx(0.4)
        assert '"trend"' in (row.category_distribution_json or "")

    def test_m2_nan_becomes_none(self, db_session, run_row):
        SVC.persist_generation(db_session, run_id=run_row.id, generation=0,
                               record={"population_size": 10,
                                       "diversity_health": float("nan"),
                                       "actual_mutation_rate": float("inf")})
        row = SVC.load_generation(db_session, run_id=run_row.id, generation=0)
        assert row.diversity_health is None
        assert row.actual_mutation_rate is None

    def test_full_m2_loop_persists_all_generations(self, db_session, run_row):
        res = GA.run_ga_loop(
            _cfg(selection_mode="advanced", adaptive=True, max_generations=3),
            _population(10), evaluate=_improving_evaluator())
        for gen, record in enumerate(res.history):
            SVC.persist_generation(db_session, run_id=run_row.id,
                                   generation=gen, record=record)
        rows = db_session.query(FactorMiningGeneration).filter_by(
            run_id=run_row.id).order_by(FactorMiningGeneration.generation).all()
        assert len(rows) == res.generations_run
        for row in rows:
            assert row.adaptive_state in ("early", "mid", "late",
                                          "stall_rescue", "fast_progress")
            assert row.pareto_front_count >= 1
            # P0：float 列必须有限或 None
            for col in ("diversity_health", "category_evenness",
                        "actual_mutation_rate"):
                v = getattr(row, col)
                assert v is None or math.isfinite(v), (col, v)


# ══════════════════════════════════════════════════════════
# 9. task_runner：选择路径推导 / 随机探索层 / 注入供给
# ══════════════════════════════════════════════════════════


class TestTaskRunnerHelpers:
    @pytest.mark.parametrize("evo,expected", [
        ({}, "naive"),
        ({"adaptive": True}, "advanced"),
        ({"adaptive": False}, "naive"),
        ({"tournament_k": 3}, "advanced"),
        ({"objectives": ["icir", "coverage", "turnover", "complexity"]},
         "advanced"),
        ({"cross_category_ratio": 0.3}, "advanced"),
        ({"selection_mode": "naive", "adaptive": True}, "naive"),
        ({"selection_mode": "advanced"}, "advanced"),
        ({"selection_mode": " ADVANCED "}, "advanced"),
        ({"mutation_rate": 0.55}, "naive"),
    ])
    def test_resolve_selection_mode(self, evo, expected):
        assert TR._resolve_selection_mode(evo) == expected

    def test_fill_random_exploration_fills_remaining(self):
        population = [
            {"formula": "mean(close,20)", "canonical_formula": "mean(close,20)",
             "formula_hash": "c1", "operation": "enumerated"},
            {"formula": "stddev(close,10)", "canonical_formula": "stddev(close,10)",
             "formula_hash": "c2", "operation": "enumerated"},
        ]
        added = TR._fill_random_exploration(
            population, target_size=10,
            selected_fields=["close", "volume", "amount", "turnover_rate"],
            seed=42)
        assert added > 0, "随机探索层应填充剩余名额"
        assert len(population) > 2
        for item in population[2:]:
            assert item["operation"] == "random"
            assert item["generation"] == 0
            assert item["formula_hash"]

    def test_fill_random_exploration_noop_when_full(self):
        population = [{"formula": "close", "canonical_formula": "close",
                       "formula_hash": "c1"}] * 5
        assert TR._fill_random_exploration(
            population, target_size=5, selected_fields=["close"], seed=1) == 0

    def test_make_random_supplier_deterministic_and_distinct(self):
        supply = TR._make_random_supplier(
            selected_fields=["close", "volume", "amount", "turnover_rate"],
            base_seed=7, existing_formulas=("mean(close,20)",))
        batch1 = supply(3)
        assert len(batch1) == 3
        # 同参数重建 → 首批完全一致（固定种子可复现）
        supply2 = TR._make_random_supplier(
            selected_fields=["close", "volume", "amount", "turnover_rate"],
            base_seed=7, existing_formulas=("mean(close,20)",))
        assert [b.get("formula") for b in supply2(3)] == \
            [b.get("formula") for b in batch1]
        # 已见公式记忆：第二批不与第一批重复
        batch2 = supply(3)
        seen = {b.get("formula") for b in batch1}
        assert not (seen & {b.get("formula") for b in batch2})

    def test_make_random_supplier_failure_returns_empty(self):
        # 无可用字段（类型表外）→ 生成器跳过 → 空列表（不抛异常）
        supply = TR._make_random_supplier(
            selected_fields=[], base_seed=7, existing_formulas=())
        assert supply(3) == []
