"""第 4 层运行时相关性去重（P1-6）契约测试。

覆盖
====
- `spearman_rho`：完全正相关 / 完全负相关 / 样本不足返回 NaN
- `factor_value_signature`：降采样 + factor_std（截图百分位秩，∈[0,0.289]）+
  Top20% MinHash 向量；空面板返回 None
- `dedup_by_correlation`：三层指纹漏斗 ——
  （1）同 category 高相关两个体 → 后一个标 `eliminated_similar`（|ρ|≥0.95）；
  （2）不同 category → 不合并；（3）统计指纹 3 项差异大 → 跳过
- `genetic_algorithm.run_ga_loop(runtime_dedup=...)` 集成：
  淘汰个体不进繁殖池，`total_trials` 仍按**全量评估数**累加（DSR 纪律）
- `service.persist_candidates`：淘汰元数据 + `FactorMiningPrescreenFingerprint` 落库
"""
from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningPrescreenFingerprint,
    FactorMiningRun,
)
from app.services.factors.mining import genetic_algorithm as GA
from app.services.factors.mining import runtime_correlation as RC
from app.services.factors.mining import service as SVC
from app.services.factors.mining.dedup import ELIM_REASON_SIMILAR


# ══════════════════════════════════════════════════════════
# 测试基建
# ══════════════════════════════════════════════════════════


def _panel(n_dates: int = 40, n_symbols: int = 100, *, seed: int = 0,
           scale: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        rng.normal(0.0, 1.0, size=(n_dates, n_symbols)) * scale,
        index=pd.Index([f"d{i:02d}" for i in range(n_dates)]),
        columns=pd.Index([f"s{j:03d}" for j in range(n_symbols)]),
    )


def _entry(formula_hash: str, panel: pd.DataFrame, *, category: str = "trend",
           direction: str = "positive", ic_mean: float = 0.01,
           turnover: float = 0.2) -> dict:
    return {
        "formula_hash": formula_hash,
        "formula": formula_hash,
        "category": category,
        "expected_direction": direction,
        "fitness": {"icir": 0.5, "coverage": 0.8, "turnover": turnover,
                    "ic_mean": ic_mean, "complexity": 5},
        "signature": RC.factor_value_signature(panel),
    }


@pytest.fixture
def run_row(db_session):
    row = FactorMiningRun(
        id="run-rc-1", status="running", candidate_pool_snapshot_id="snap-1",
        data_cutoff_at=datetime(2026, 9, 17), start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 9, 1), rebalance_frequency="weekly",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        max_generation=5, total_trials=0,
    )
    db_session.add(row)
    db_session.commit()
    return row


# ══════════════════════════════════════════════════════════
# 1. spearman_rho
# ══════════════════════════════════════════════════════════


class TestSpearman:
    def test_perfect_positive(self):
        x = pd.Series(np.arange(50.0))
        assert RC.spearman_rho(x, 3 * x + 1) == pytest.approx(1.0)

    def test_perfect_negative(self):
        x = pd.Series(np.arange(50.0))
        assert RC.spearman_rho(x, -x) == pytest.approx(-1.0)

    def test_insufficient_samples_returns_nan(self):
        x = pd.Series([1.0, 2.0])
        assert math.isnan(RC.spearman_rho(x, x, min_n=30))

    def test_short_input_returns_nan(self):
        x = pd.Series([1.0])
        assert math.isnan(RC.spearman_rho(x, x))


# ══════════════════════════════════════════════════════════
# 2. factor_value_signature
# ══════════════════════════════════════════════════════════


class TestSignature:
    def test_builds_signature(self):
        sig = RC.factor_value_signature(_panel(seed=1))
        assert sig is not None
        assert not sig.empty
        # rank 口径 scale-free；有限样本 std 略高于理论均匀上界 1/√12≈0.289
        assert 0.0 < sig.factor_std <= 0.3
        vec = json.loads(sig.top_overlap_vector)
        assert isinstance(vec, list) and len(vec) > 0

    def test_empty_panel_returns_none(self):
        assert RC.factor_value_signature(pd.DataFrame()) is None

    def test_downsamples_to_limits(self):
        sig = RC.factor_value_signature(_panel(n_dates=60, n_symbols=400, seed=1))
        assert sig.panel.shape[0] <= RC.MAX_SIGNATURE_DATES
        assert sig.panel.shape[1] <= RC.MAX_SIGNATURE_SYMBOLS

    def test_scale_invariant_factor_std(self):
        """rank 口径：因子值整体放大不改变 factor_std（尺度无关）。"""
        a = RC.factor_value_signature(_panel(seed=1))
        b = RC.factor_value_signature(_panel(seed=1, scale=100.0))
        assert a.factor_std == pytest.approx(b.factor_std)


# ══════════════════════════════════════════════════════════
# 3. dedup_by_correlation 漏斗
# ══════════════════════════════════════════════════════════


class TestDedupFunnel:
    def test_high_correlation_marks_similar(self):
        a = _panel(seed=1)
        entries = [_entry("f1", a), _entry("f2", 2.0 * a)]
        result = RC.dedup_by_correlation(entries, seed=42)
        assert result.stats["input_count"] == 2
        assert len(result.kept) == 1
        assert len(result.eliminated) == 1
        eliminated = result.eliminated[0]
        assert eliminated["elimination_status"] == "eliminated"
        assert eliminated["elimination_reason"] == ELIM_REASON_SIMILAR
        assert eliminated["similar_to_candidate_id"] == "f1"
        assert eliminated["similarity_score"] >= RC.CORRELATION_THRESHOLD
        kept = result.kept[0]
        assert kept["elimination_status"] == "active"

    def test_different_category_not_merged(self):
        a = _panel(seed=1)
        entries = [_entry("f1", a, category="trend"),
                   _entry("f2", 2.0 * a, category="value")]
        result = RC.dedup_by_correlation(entries, seed=42)
        assert len(result.eliminated) == 0
        assert len(result.kept) == 2

    def test_stat_fingerprint_3_diff_skips(self):
        """多空方向 / IC 均值 / 换手率 3 项差异大 → 跳过精确 Spearman。"""
        a = _panel(seed=1)
        e1 = _entry("f1", a, direction="positive", ic_mean=0.01, turnover=0.2)
        e2 = _entry("f2", 2.0 * a, direction="negative", ic_mean=0.05,
                    turnover=0.9)
        result = RC.dedup_by_correlation([e1, e2], seed=42)
        assert len(result.eliminated) == 0

    def test_uncorrelated_not_merged(self):
        a = _panel(seed=1)
        c = _panel(seed=999)
        entries = [_entry("f1", a), _entry("f2", c)]
        result = RC.dedup_by_correlation(entries, seed=42)
        assert len(result.eliminated) == 0

    def test_fingerprints_and_stats(self):
        result = RC.dedup_by_correlation([_entry("f1", _panel(seed=1))],
                                         seed=42)
        assert result.stats["input_count"] == 1
        assert result.stats["kept_count"] == 1
        assert result.stats["dedup_rate"] == 0.0
        fp = result.fingerprints[0]
        assert fp["semantic_category"] == "trend"
        assert fp["long_short_direction"] == 1
        assert fp["ic_mean_short"] == pytest.approx(0.01)
        assert fp["turnover_rate"] == pytest.approx(0.2)
        assert fp["factor_std"] is not None
        assert fp["top_overlap_vector"] is not None


class TestBuildFingerprint:
    def test_assembles_fields(self):
        sig = RC.factor_value_signature(_panel(seed=1))
        fp = RC.build_fingerprint(
            {"category": "momentum", "expected_direction": "negative",
             "fitness": {"ic_mean": 0.02, "turnover": 0.33}}, sig)
        assert fp["semantic_category"] == "momentum"
        assert fp["long_short_direction"] == -1
        assert fp["ic_mean_short"] == pytest.approx(0.02)
        assert fp["turnover_rate"] == pytest.approx(0.33)
        assert fp["factor_std"] == pytest.approx(sig.factor_std)
        assert fp["top_overlap_vector"] == sig.top_overlap_vector


# ══════════════════════════════════════════════════════════
# 4. GA 集成：runtime_dedup 注入点 + total_trials 纪律
# ══════════════════════════════════════════════════════════


def _population(n: int, *, prefix: str = "f") -> list[dict]:
    return [{"formula": f"mean(close,{10 + i*10})-{i}",
             "canonical_formula": f"mean(close,{10 + i*10})-{i}",
             "formula_hash": f"{prefix}-hash-{i}",
             "source": "classic", "operation": "enumerated",
             "generation": 0} for i in range(n)]


def _flat_evaluator(value: float = 0.5):
    def _evaluate(item):
        return {"icir": value, "coverage": 0.8, "turnover": 0.2,
                "ic_mean": 0.01, "complexity": 5}
    return _evaluate


def _cfg(**kw) -> GA.GAConfig:
    base = {"population_size": 10, "max_generations": 3,
            "selection_ratio": 0.2, "mutation_rate": 0.55,
            "crossover_rate": 0.25, "random_rate": 0.20,
            "convergence_threshold": 1e-3, "convergence_generations": 2,
            "seed": 7}
    base.update(kw)
    return GA.GAConfig(**base)


def _mark_half_similar(ranked):
    """stub：把排名后一半标 `eliminated_similar`（模拟第 4 层去重）。"""
    half = len(ranked) // 2
    rep_hash = ranked[0].get("formula_hash") if ranked else ""
    for e in ranked[:half]:
        e["elimination_status"] = "active"
        e["elimination_reason"] = None
    for e in ranked[half:]:
        e["elimination_status"] = "eliminated"
        e["elimination_reason"] = ELIM_REASON_SIMILAR
        e["similar_to_candidate_id"] = rep_hash
        e["similarity_score"] = 0.99


class TestGAIntegration:
    def test_total_trials_counts_full_evaluation(self):
        """去重只减繁殖池，不减 DSR 输入源（total_trials = Σ 种群大小）。"""
        res = GA.run_ga_loop(_cfg(), _population(10),
                             evaluate=_flat_evaluator(0.6),
                             runtime_dedup=_mark_half_similar)
        assert res.total_trials == 10 * res.generations_run, \
            f"total_trials 应按全量评估数累加，实得 {res.total_trials}"

    def test_eliminated_reaches_on_generation(self):
        seen: list = []
        GA.run_ga_loop(_cfg(), _population(10), evaluate=_flat_evaluator(0.6),
                       runtime_dedup=_mark_half_similar,
                       on_generation=lambda g, rec, ranked: seen.append(ranked))
        last = seen[-1]
        eliminated = [e for e in last
                      if e.get("elimination_reason") == ELIM_REASON_SIMILAR]
        assert eliminated, "淘汰标记应到达 on_generation 的 ranked"
        assert all(e.get("similar_to_candidate_id") for e in eliminated)

    def test_dedup_exception_degrades_gracefully(self):
        """去重回调抛异常 → 退化为全量繁殖，不阻断进化。"""

        def boom(ranked):
            raise RuntimeError("去重爆炸")

        res = GA.run_ga_loop(_cfg(), _population(10),
                             evaluate=_flat_evaluator(0.6),
                             runtime_dedup=boom)
        assert res.generations_run >= 1


# ══════════════════════════════════════════════════════════
# 5. 落库：淘汰元数据 + 预筛指纹
# ══════════════════════════════════════════════════════════


class TestPersistence:
    def test_persist_candidates_fingerprint_and_elimination(self, db_session, run_row):
        sig = RC.factor_value_signature(_panel(seed=1))
        fp = RC.build_fingerprint(
            {"category": "trend", "expected_direction": "positive",
             "fitness": {"ic_mean": 0.01, "turnover": 0.2}}, sig)
        ranked = [{
            "formula": "mean(close,20)",
            "canonical_formula": "mean(close,20)",
            "formula_hash": "h1",
            "operation": "elite",
            "category": "trend",
            "expected_direction": "positive",
            "fitness": {"icir": 0.5, "coverage": 0.8, "turnover": 0.2,
                        "ic_mean": 0.01, "complexity": 5},
            "_prescreen_fingerprint": fp,
            "elimination_status": "eliminated",
            "elimination_reason": "eliminated_similar",
            "similar_to_candidate_id": "h0",
            "similarity_score": 0.97,
        }]
        inserted = SVC.persist_candidates(db_session, run_id=run_row.id,
                                          generation=1, ranked=ranked)
        assert inserted == 1

        cand = db_session.query(FactorMiningCandidate).filter_by(
            run_id=run_row.id, formula_hash="h1").one()
        assert cand.elimination_status == "eliminated"
        assert cand.elimination_reason == "eliminated_similar"
        assert cand.similar_to_candidate_id == "h0"
        assert cand.similarity_score == pytest.approx(0.97)

        fp_row = db_session.query(FactorMiningPrescreenFingerprint).filter_by(
            candidate_id=cand.id).one()
        assert fp_row.semantic_category == "trend"
        assert fp_row.long_short_direction == 1
        assert fp_row.ic_mean_short == pytest.approx(0.01)
        assert fp_row.turnover_rate == pytest.approx(0.2)
        assert fp_row.factor_std == pytest.approx(sig.factor_std)
        assert fp_row.top_overlap_vector == sig.top_overlap_vector

    def test_persist_candidates_active_has_no_fingerprint(self, db_session, run_row):
        """无 `_prescreen_fingerprint`（naive 未接去重）→ 不写指纹行。"""
        ranked = [{"formula": "mean(close,20)",
                   "canonical_formula": "mean(close,20)",
                   "formula_hash": "h2", "operation": "elite"}]
        inserted = SVC.persist_candidates(db_session, run_id=run_row.id,
                                          generation=0, ranked=ranked)
        assert inserted == 1
        cand = db_session.query(FactorMiningCandidate).filter_by(
            run_id=run_row.id, formula_hash="h2").one()
        count = db_session.query(FactorMiningPrescreenFingerprint).filter_by(
            candidate_id=cand.id).count()
        assert count == 0