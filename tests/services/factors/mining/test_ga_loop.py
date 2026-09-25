"""T23 · GA 主循环（朴素）契约测试（DoD）。

覆盖（对齐任务卡四条坑）
======================
- **收敛判断 = 连续 N 代提升 < 阈值**（不是固定 ICIR 门禁）——
  「低 ICIR 区间也必须正常收敛」的专项用例
- **`total_trials` 每代累加**（Σ 种群大小），并写回 `factor_mining_runs`
- **精英 `operation='elite'` 且不参与繁殖**（血统不被重复计次）
- **数值写入安全**：`persist_*` 全部经 `db_numeric`；NaN/Inf 进不来
  （用 `db_session` fixture 落库 + **真实库试探**在 T21/P0 已覆盖，此处验契约）
- 三率配额与护栏；评估异常隔离；收敛早停；种群大小稳定
"""
from __future__ import annotations

import math
import re
from datetime import datetime

import pytest

from app.core import db_numeric as DN
from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningGeneration,
    FactorMiningRun,
)
from app.services.factors.mining import genetic_algorithm as GA
from app.services.factors.mining import service as SVC


# ══════════════════════════════════════════════════════════
# 测试基建
# ══════════════════════════════════════════════════════════


def _population(n: int, *, prefix: str = "f") -> list[dict]:
    """构造初始种群（朴素公式，参数可被变异/交叉操作）。"""
    return [{"formula": f"mean(close,{10 + i*10})-{i}",
             "canonical_formula": f"mean(close,{10 + i*10})-{i}",
             "formula_hash": f"{prefix}-hash-{i}",
             "source": "classic", "operation": "enumerated",
             "generation": 0} for i in range(n)]


def _evaluator_by_rank(values: list[float]):
    """按公式序号给 ICIR（便于构造「逐代提升/停滞」场景）。"""

    def _evaluate(item):
        index = int(str(item["formula"]).rsplit("-", 1)[-1])
        return {"icir": values[index % len(values)], "coverage": 0.8,
                "turnover": 0.2, "ic_mean": 0.01, "complexity": 5}

    return _evaluate


def _flat_evaluator(value: float):
    def _evaluate(item):
        return {"icir": value, "coverage": 0.8, "turnover": 0.2,
                "ic_mean": 0.01, "complexity": 5}
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
    """建一条 run 行（generation 表有 FK 指向它）。"""
    row = FactorMiningRun(
        id="run-ga-1", status="running", candidate_pool_snapshot_id="snap-1",
        data_cutoff_at=datetime(2026, 9, 17), start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 9, 1), rebalance_frequency="weekly",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        max_generation=5, total_trials=0,
    )
    db_session.add(row)
    db_session.commit()
    return row


# ══════════════════════════════════════════════════════════
# 1. 配置校验（三率护栏）
# ══════════════════════════════════════════════════════════


class TestGAConfig:
    def test_valid_config(self):
        GA.GAConfig()     # 默认值合法

    def test_rates_must_sum_to_one(self):
        with pytest.raises(ValueError, match="三率之和"):
            _cfg(mutation_rate=0.5, crossover_rate=0.25, random_rate=0.15)

    @pytest.mark.parametrize("kw", [
        {"mutation_rate": 0.30},          # < 0.40 护栏
        {"mutation_rate": 0.85},          # > 0.80
        {"crossover_rate": 0.05},         # < 0.10
        {"crossover_rate": 0.55},         # > 0.50
        {"random_rate": 0.02},            # < 0.05
        {"random_rate": 0.25},            # > 0.20
    ])
    def test_guardrails(self, kw):
        """需求 §6.1.2 安全护栏：变异 [0.40,0.80] / 交叉 [0.10,0.50] / 注入 [0.05,0.20]。"""
        rates = {"mutation_rate": 0.55, "crossover_rate": 0.25,
                 "random_rate": 0.20}
        rates.update(kw)
        # 保持三率和为 1（只改一个时同步改其它）
        rates["mutation_rate"] = kw.get("mutation_rate",
                                        rates["mutation_rate"])
        rest = 1.0 - rates["mutation_rate"]
        rates["crossover_rate"] = kw.get(
            "crossover_rate", min(0.50, rest * 0.4))
        rates["random_rate"] = 1.0 - rates["mutation_rate"] - rates["crossover_rate"]
        with pytest.raises(ValueError, match="护栏"):
            _cfg(**rates)

    def test_selection_ratio_bounds(self):
        with pytest.raises(ValueError, match="selection_ratio"):
            _cfg(selection_ratio=1.0)
        with pytest.raises(ValueError, match="selection_ratio"):
            _cfg(selection_ratio=0.0)

    def test_population_floor(self):
        with pytest.raises(ValueError, match="population_size"):
            _cfg(population_size=3)


# ══════════════════════════════════════════════════════════
# 2. 朴素算子（参数级）
# ══════════════════════════════════════════════════════════


class TestOperators:
    def test_mutate_changes_params_keeps_structure(self):
        import random

        rng = random.Random(1)
        out = GA.mutate_formula("mean(close,20)-mean(close,60)", rng,
                                intensity=0.5)
        assert out != "mean(close,20)-mean(close,60)", "至少一个参数应被扰动"
        assert out.count("mean(") == 2, "结构不得改变（M1 参数级变异）"

    def test_mutate_params_stay_in_window(self):
        import random

        rng = random.Random(3)
        for _ in range(50):
            out = GA.mutate_formula("mean(close,5)-mean(close,250)", rng,
                                    intensity=0.9)
            nums = [int(x) for x in re.findall(r"\d+", out)]
            assert all(1 <= n <= 250 for n in nums), out

    def test_crossover_mixes_params_keeps_structure(self):
        """多 seed 验证：结构恒与 A 一致；参数混合（至少一个 seed 产生差异）。"""
        import random

        differed = False
        for seed in range(8):
            rng = random.Random(seed)
            child = GA.crossover_formulas("mean(close,20)-mean(close,60)",
                                          "mean(close,10)-mean(close,120)", rng)
            assert child.count("mean(") == 2, child
            if child != "mean(close,20)-mean(close,60)":
                differed = True
        assert differed, "至少一个 seed 应产生混合参数"

    def test_crossover_no_numbers_returns_a(self):
        import random

        rng = random.Random(2)
        assert GA.crossover_formulas("cs_rank(close)", "cs_rank(volume)",
                                     rng) == "cs_rank(close)"

    def test_mutate_formula_without_numbers(self):
        import random

        assert GA.mutate_formula("cs_rank(close)", random.Random(1)) == \
            "cs_rank(close)"


# ══════════════════════════════════════════════════════════
# 3. 主循环：选择 / 配额 / 精英
# ══════════════════════════════════════════════════════════


class TestLoopMechanics:
    def test_runs_all_generations_with_improvement(self):
        """有持续提升 → 跑满 max_generations（收敛只看「提升量」）。"""
        gen_counter = {"n": 0}

        def evaluate(item):
            gen_counter["n"] += 1
            return {"icir": 0.5 + 0.1 * (gen_counter["n"] // 10),
                    "coverage": 0.8, "turnover": 0.2, "ic_mean": 0.01,
                    "complexity": 5}

        res = GA.run_ga_loop(_cfg(max_generations=4), _population(10),
                             evaluate=evaluate)
        assert res.generations_run == 4
        assert res.stopped_reason == "max_generations"

    def test_flat_icir_converges_early(self):
        """ICIR 平坦 → 提升为 0 → 连续 2 代无提升即收敛（任务卡坑 1）。"""
        res = GA.run_ga_loop(_cfg(max_generations=4), _population(10),
                             evaluate=_flat_evaluator(0.5))
        assert res.stopped_reason == "converged"
        assert res.generations_run == 3, "基线代 + 连续 2 代无提升"

    def test_empty_population_rejected(self):
        with pytest.raises(ValueError, match="初始种群为空"):
            GA.run_ga_loop(_cfg(), [], evaluate=_flat_evaluator(0.5))

    def test_total_trials_accumulates(self):
        """🚨 任务卡坑 2：total_trials 每代累加（DSR 唯一输入源）。"""
        cfg = _cfg(population_size=10, max_generations=4)
        res = GA.run_ga_loop(cfg, _population(10),
                             evaluate=_flat_evaluator(0.5))
        # 平坦 ICIR 会提前收敛（坑 1 的正确行为）—— 断言「每代都累加」而非具体值
        assert res.total_trials == 10 * res.generations_run, \
            f"total_trials 应为 Σ种群大小 = {10 * res.generations_run}，实得 {res.total_trials}"
        trials = [h["total_trials"] for h in res.history]
        assert trials == sorted(trials) and len(set(trials)) == len(trials)

    def test_elites_marked_and_not_bred(self):
        """🚨 任务卡坑 3：精英 operation='elite' 且不参与繁殖（血统不被重复计次）。"""
        records: list = []

        def on_generation(gen, record, ranked):
            records.append((gen, [dict(r, rank=i) for i, r in enumerate(ranked)]))

        GA.run_ga_loop(_cfg(max_generations=2, selection_ratio=0.2),
                       _population(10),
                       evaluate=_flat_evaluator(0.5), on_generation=on_generation)
        # 第 2 代的初始种群 = 上一代精英 + 子代；精英保持 elite 标记
        gen2_initial = records[1][1]
        elites = [r for r in gen2_initial if r.get("operation") == "elite"]
        assert elites, "上一代精英应原样进入下一代"
        assert all(r["operation"] != OP for r in elites
                   for OP in ("mutation", "crossover", "random")), \
            "精英不得被再次繁殖"

    def test_elite_count_follows_selection_ratio(self):
        records: list = []
        GA.run_ga_loop(_cfg(population_size=10, selection_ratio=0.3,
                            max_generations=1),
                       _population(10), evaluate=_flat_evaluator(0.5),
                       on_generation=lambda g, rec, ranked: records.append(rec))
        assert records[0]["elite_count"] == 3, "0.3 × 10 = 3 个精英"

    def test_operation_counts_sum_to_slots(self):
        records: list = []
        GA.run_ga_loop(_cfg(population_size=10, selection_ratio=0.2,
                            max_generations=1),
                       _population(10), evaluate=_flat_evaluator(0.5),
                       on_generation=lambda g, rec, ranked: records.append(rec))
        rec = records[0]
        non_elite = 10 - rec["elite_count"]
        assert (rec["mutation_count"] + rec["crossover_count"]
                + rec["random_count"]) == non_elite, \
            "三率配额必须恰好覆盖非精英名额"

    def test_evaluation_exception_isolated(self):
        """个别个体评估失败 → 该个体淘汰，**整代不中断**。"""
        calls = {"n": 0}

        def evaluate(item):
            calls["n"] += 1
            if "hash-0" in str(item.get("formula_hash")):
                raise RuntimeError("评估爆炸")
            return {"icir": 0.5, "coverage": 0.8, "turnover": 0.2,
                    "ic_mean": 0.01, "complexity": 5}

        records: list = []
        res = GA.run_ga_loop(_cfg(max_generations=1),
                             _population(10), evaluate=evaluate,
                             on_generation=lambda g, rec, r: records.append(rec))
        assert res.generations_run == 1
        assert res.history[0]["eliminated_count"] == 1

    def test_ranking_by_icir_desc(self):
        """朴素选择 = 按 ICIR 降序（M1）。"""
        records: list = []
        values = [0.1, 0.9, 0.5, 0.7, 0.3, 0.2, 0.8, 0.4, 0.6, 0.05]

        def evaluate(item):
            index = int(str(item["formula"]).rsplit("-", 1)[-1])
            return {"icir": values[index], "coverage": 0.8, "turnover": 0.2,
                    "ic_mean": 0.01, "complexity": 5}

        GA.run_ga_loop(_cfg(max_generations=1), _population(10),
                       evaluate=evaluate,
                       on_generation=lambda g, rec, ranked: records.append(ranked))
        icirs = [r["fitness"]["icir"] for r in records[0]]
        assert icirs == sorted(icirs, reverse=True)

    def test_population_size_stable(self):
        """每代种群大小应稳定（精英 + 子代 = 配置值）。"""
        sizes: list = []
        GA.run_ga_loop(_cfg(population_size=12, max_generations=3),
                       _population(12), evaluate=_flat_evaluator(0.5),
                       on_generation=lambda g, rec, ranked: sizes.append(rec["population_size"]))
        assert sizes == [12, 12, 12]


# ══════════════════════════════════════════════════════════
# 4. 收敛判断（🚨 任务卡坑 1：连续 N 代提升 < 阈值）
# ══════════════════════════════════════════════════════════


class TestConvergence:
    def test_flat_icir_converges(self):
        """ICIR 完全平坦（提升=0 < 阈值）→ 连续 N 代后停止，reason=converged。"""
        res = GA.run_ga_loop(_cfg(max_generations=10, convergence_generations=2),
                             _population(10), evaluate=_flat_evaluator(0.5))
        assert res.stopped_reason == "converged"
        assert res.generations_run == 3, "gen0 基线 + 连续 2 代无提升 → 第 3 代停"

    def test_steady_improvement_does_not_converge(self):
        """逐代提升 ≥ 阈值 → 不收敛，跑满 max_generations。"""
        gen_counter = {"n": 0}

        def evaluate(item):
            gen_counter["n"] += 1
            return {"icir": 0.1 + 0.1 * (gen_counter["n"] // 10),
                    "coverage": 0.8, "turnover": 0.2, "ic_mean": 0.01,
                    "complexity": 5}

        res = GA.run_ga_loop(_cfg(max_generations=5, convergence_threshold=1e-3),
                             _population(10), evaluate=evaluate)
        assert res.stopped_reason == "max_generations"
        assert res.generations_run == 5

    def test_low_icir_plateau_still_converges(self):
        """🚨 反「固定 ICIR 门禁」专项：ICIR 只有 0.001（远低于任何门禁），
        只要**提升 < 阈值**也必须正常收敛 —— 收敛看的是「变化量」，不是绝对值。"""
        res = GA.run_ga_loop(
            _cfg(max_generations=10, convergence_generations=2,
                 convergence_threshold=1e-3),
            _population(10), evaluate=_flat_evaluator(0.001))
        assert res.stopped_reason == "converged"
        assert res.generations_run == 3

    def test_alternating_improvement_resets_stall(self):
        """提升 → 停滞 → 提升：stall 应被重置（连续性要求）。"""
        seq = [0.5, 0.6, 0.6, 0.7, 0.7, 0.8, 0.8, 0.9, 0.9, 1.0]
        gen_counter = {"n": 0}

        def evaluate(item):
            gen_counter["n"] += 1
            return {"icir": seq[(gen_counter["n"] // 10) % len(seq)],
                    "coverage": 0.8, "turnover": 0.2, "ic_mean": 0.01,
                    "complexity": 5}

        res = GA.run_ga_loop(
            _cfg(max_generations=9, convergence_generations=2,
                 convergence_threshold=1e-3),
            _population(10), evaluate=evaluate)
        # 每代都有提升（0.1）→ 不收敛
        assert res.stopped_reason == "max_generations"
        assert res.stall_count == 0


# ══════════════════════════════════════════════════════════
# 5. 随机注入
# ══════════════════════════════════════════════════════════


class TestRandomInjection:
    def test_random_supplier_called_with_count(self):
        requests: list = []

        def supplier(count):
            requests.append(count)
            return [{"formula": f"cs_rank(close,{count})", "source": "random",
                     "operation": "random", "formula_hash": f"r{count}-{i}"}
                    for i in range(count)]

        records: list = []
        GA.run_ga_loop(_cfg(population_size=10, selection_ratio=0.2,
                            random_rate=0.20, max_generations=1),
                       _population(10), evaluate=_flat_evaluator(0.5),
                       random_supplier=supplier,
                       on_generation=lambda g, rec, ranked: records.append(rec))
        assert requests and all(n >= 1 for n in requests)
        assert records[0]["random_count"] >= 1

    def test_injected_items_marked_random(self):
        def supplier(count):
            return [{"formula": f"cs_rank(close)", "source": "random",
                     "operation": "random"} for _ in range(count)]

        res = GA.run_ga_loop(_cfg(population_size=10, max_generations=1),
                             _population(10), evaluate=_flat_evaluator(0.5),
                             random_supplier=supplier)
        assert res.history[0]["random_count"] >= 1

    def test_no_supplier_falls_back_to_mutation(self):
        """未提供随机供给 → 变异兜底补齐名额（种群大小不变）。"""
        sizes: list = []
        GA.run_ga_loop(_cfg(population_size=10, max_generations=2),
                       _population(10), evaluate=_flat_evaluator(0.5),
                       on_generation=lambda g, rec, ranked: sizes.append(
                           rec["population_size"]))
        assert sizes == [10, 10]


# ══════════════════════════════════════════════════════════
# 6. 落库（db_session fixture：SQLite；**真实库试探见下**）
# ══════════════════════════════════════════════════════════


class TestPersistence:
    def test_persist_generation_creates_and_updates(self, db_session, run_row):
        row = SVC.persist_generation(db_session, run_id=run_row.id, generation=0,
                                     record={"population_size": 10,
                                             "best_icir": 0.5, "avg_icir": 0.3,
                                             "median_icir": 0.25,
                                             "elite_count": 2,
                                             "mutation_count": 4,
                                             "crossover_count": 2,
                                             "random_count": 2,
                                             "total_trials": 10,
                                             "stall_count": 0,
                                             "eliminated_count": 0})
        assert row.generation == 0
        assert row.population_size == 10
        assert row.best_icir == pytest.approx(0.5)
        # 再写一代（更新行数不变）
        SVC.persist_generation(db_session, run_id=run_row.id, generation=1,
                               record={"population_size": 10})
        rows = db_session.query(FactorMiningGeneration).filter_by(
            run_id=run_row.id).all()
        assert len(rows) == 2

    def test_persist_generation_nan_becomes_none(self, db_session, run_row):
        """🚨 P0：NaN 必须转 None（写 NaN 会让整条 INSERT 失败）。"""
        SVC.persist_generation(db_session, run_id=run_row.id, generation=0,
                               record={"population_size": 10,
                                       "best_icir": float("nan"),
                                       "avg_icir": float("inf")})
        row = SVC.load_generation(db_session, run_id=run_row.id, generation=0)
        assert row.best_icir is None and row.avg_icir is None

    def test_sync_run_progress_accumulates(self, db_session, run_row):
        SVC.sync_run_progress(db_session, run_id=run_row.id, generation=1,
                              total_trials=10)
        SVC.sync_run_progress(db_session, run_id=run_row.id, generation=2,
                              total_trials=25)
        db_session.refresh(run_row)
        assert run_row.current_generation == 2
        assert run_row.total_trials == 25

    def test_sync_run_progress_unknown_run(self, db_session):
        with pytest.raises(ValueError, match="run 不存在"):
            SVC.sync_run_progress(db_session, run_id="nope", generation=0,
                                  total_trials=0)

    def test_persist_candidates(self, db_session, run_row):
        ranked = [{"formula": "mean(close,20)", "canonical_formula": "mean(close,20)",
                   "formula_hash": "h1", "operation": "elite", "category": "trend",
                   "fitness": {"icir": 0.5, "coverage": 0.8, "turnover": 0.2,
                               "complexity": 5}},
                  {"formula": "cs_rank(pe_ttm)", "canonical_formula": "cs_rank(pe_ttm)",
                   "formula_hash": "h2", "operation": "mutation", "category": "valuation",
                   "fitness": {"icir": float("nan"), "coverage": 0.8,
                               "turnover": 0.2, "complexity": 3}}]
        inserted = SVC.persist_candidates(db_session, run_id=run_row.id,
                                          generation=1, ranked=ranked)
        assert inserted == 2
        rows = db_session.query(FactorMiningCandidate).filter_by(
            run_id=run_row.id).order_by(FactorMiningCandidate.formula_hash).all()
        assert len(rows) == 2
        by_hash = {r.formula_hash: r for r in rows}
        assert by_hash["h1"].generation_icir == pytest.approx(0.5)
        assert by_hash["h1"].generation_rank == 0
        assert by_hash["h1"].operation == "elite"
        # 🚨 NaN → None（不写 NaN，整条 INSERT 才能成功）
        assert by_hash["h2"].generation_icir is None

    def test_persist_candidates_idempotent(self, db_session, run_row):
        ranked = [{"formula": "mean(close,20)", "canonical_formula": "mean(close,20)",
                   "formula_hash": "h1", "operation": "elite"}]
        first = SVC.persist_candidates(db_session, run_id=run_row.id,
                                       generation=0, ranked=ranked)
        second = SVC.persist_candidates(db_session, run_id=run_row.id,
                                        generation=0, ranked=ranked)
        assert (first, second) == (1, 0), "重跑不得产生重复行"

    def test_create_run(self, db_session):
        row = SVC.create_run(db_session, run_id="run-create-1",
                             candidate_pool_snapshot_id="snap-1",
                             data_cutoff_at=datetime(2026, 9, 17),
                             start_date=datetime(2026, 1, 1),
                             end_date=datetime(2026, 9, 1),
                             rebalance_frequency="weekly",
                             max_generation=8, random_seed=42)
        assert row.id == "run-create-1"
        assert row.total_trials == 0
        assert row.current_generation == 0


# ══════════════════════════════════════════════════════════
# 7. 端到端：GA 循环 + 落库 + 探针口径
# ══════════════════════════════════════════════════════════


class TestEndToEnd:
    def test_full_loop_persists_generations_and_trials(self, db_session, run_row):
        res = GA.run_ga_loop(_cfg(max_generations=3), _population(10),
                             evaluate=_flat_evaluator(0.5))
        for gen, record in enumerate(res.history):
            SVC.persist_generation(db_session, run_id=run_row.id, generation=gen,
                                   record=record)
        SVC.sync_run_progress(db_session, run_id=run_row.id,
                              generation=res.generations_run - 1,
                              total_trials=res.total_trials,
                              converged=(res.stopped_reason == "converged"))
        db_session.commit()
        db_session.refresh(run_row)
        assert run_row.total_trials == 30
        assert run_row.current_generation == 2
        rows = db_session.query(FactorMiningGeneration).filter_by(
            run_id=run_row.id).all()
        assert len(rows) == 3
        for row in rows:
            # 🚨 P0：float 列必须全部有限或 None（绝不 NaN/Inf）
            for col in ("best_icir", "avg_icir", "median_icir"):
                v = getattr(row, col)
                assert v is None or math.isfinite(v), (col, v)

    def test_probe_record_from_t21_style_stats(self, db_session, run_row):
        """T21 探针记录可以直接走同一落库层（口径一致）。"""
        SVC.persist_generation(db_session, run_id=run_row.id, generation=0,
                               record={"population_size": 10,
                                       "probe_data_load_ms": 12,
                                       "probe_g2_hit_rate": 0.9,
                                       "cache_validation_passed": 1,
                                       "cache_validation_max_diff": 0.0})
        row = SVC.load_generation(db_session, run_id=run_row.id, generation=0)
        assert row.probe_data_load_ms == 12
        assert row.probe_g2_hit_rate == pytest.approx(0.9)
        assert row.cache_validation_passed == 1


# ══════════════════════════════════════════════════════════
# DEF-12/DEF-3 · 协作式停止（should_stop 检查点）
# ══════════════════════════════════════════════════════════


class TestCooperativeStop:
    def test_stop_before_first_generation(self, run_row):
        """看门狗/pause 已请求停止 → 循环立即退出：0 代、0 评估、不落库。"""
        outcome = GA.run_ga_loop(
            _cfg(max_generations=5), _population(4),
            evaluate=_flat_evaluator(0.1),
            should_stop=lambda: True,
        )
        assert outcome.stopped_reason == "stopped"
        assert outcome.generations_run == 0
        assert outcome.total_trials == 0
        assert outcome.history == []

    def test_stop_mid_generation_keeps_completed_generations(self, run_row):
        """第 3 代中途请求停止：前 2 代完整保留（checkpoint 语义），当前代不落库。

        种群演化：初始 4 个 → 繁殖后回到 cfg.population_size=10
        （elite 2 + offspring 8）。关掉收敛（convergence_generations 巨大），
        排除 flat 评估器 2 代即 converged 的干扰。
        """
        evals = {"n": 0}

        def _evaluate(item):
            evals["n"] += 1
            return {"icir": 0.1, "coverage": 0.8, "turnover": 0.2,
                    "ic_mean": 0.01, "complexity": 5}

        def _stop():
            return evals["n"] >= 18  # gen0:4 + gen1:10 + gen2 进行到一半

        outcome = GA.run_ga_loop(
            _cfg(max_generations=8, convergence_generations=999),
            _population(4), evaluate=_evaluate,
            should_stop=_stop,
        )
        assert outcome.stopped_reason == "stopped"
        assert outcome.generations_run == 2
        # 中断代（第 3 代）跑了 4 个个体但不计数：total_trials 逐代累加，
        # 被中止的代不落记录、不计入（与「部分代记录会破坏逐代完整性」一致）
        assert outcome.total_trials == 14
        assert len(outcome.history) == 2
        assert evals["n"] == 18, "停止后不得再评估任何个体"

    def test_no_should_stop_keeps_legacy_behavior(self, run_row):
        """不传 should_stop 时行为与旧版完全一致（跑满 max_generations）。"""
        outcome = GA.run_ga_loop(
            _cfg(max_generations=2, convergence_generations=999),
            _population(4),
            evaluate=_flat_evaluator(0.1),
        )
        assert outcome.stopped_reason == "max_generations"
        assert outcome.generations_run == 2
        assert outcome.total_trials == 14  # gen0: 4 + gen1: 10（繁殖后回到种群规模）
