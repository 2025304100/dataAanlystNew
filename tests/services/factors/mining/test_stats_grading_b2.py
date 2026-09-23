"""B2 · 统计层写库 + 分级消费真实数据。

DoD 对照（开发计划 B2 卡 / 验收报告 §25-26）：
- `metrics_json.stats`（8 方法）与「原始/校正 ICIR」写入链路打通
    → TestStatsWriteHook（run_evaluation 钩子，缺省不变）+ TestMiningStatsProvider
    （真实 compute_stats 闭包）
- evaluations 契约：`get_mining_evaluation` 返回 stats → TestEvaluationContract
- 分级落库断言 + D 级移出 FactorSet → TestGradingAfterFinalize
- 季度重评消费真实列（factor_versions.grade_manual_adjusted）→ TestQuarterly

说明（B2 决策记录）：
- D 级写 F1 负样本**留 B3**（与正向样本共用 formula→AST + C5 网关机制，
  避免重复造轮子；本卡完成 stats 写库 + 分级落库 + FactorSet 移出）。
- 「校正 ICIR」当前无额外多重检验调整项，与原始一致（诚实相等，不臆造数字）。

跑法：`.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_stats_grading_b2.py -q`
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.models.factor import Factor
from app.models.factor_evaluation import EvaluationRun, FactorSet, FactorSetMember
from app.models.factor_grade_history import FactorGradeHistory
from app.models.factor_mining import FactorMiningCandidate, FactorMiningRun
from app.models.factor_model import FactorVersion
from app.schemas.factor_library import FactorSetCreate, FactorSetMemberCreate
from app.services.factors import factor_set_service as FSS
from app.services.factors.factor_evaluator import (
    EvaluationConfig,
    get_evaluation_run,
    run_evaluation,
)
from app.services.factors.mining import service as SVC


# ══════════════════════════════════════════════════════════
# 基建
# ══════════════════════════════════════════════════════════


def _make_factor_and_version(db):
    f = Factor(code="B2F", name="B2 factor", category="trend",
               direction="positive", status="active")
    db.add(f)
    db.flush()
    v = FactorVersion(factor_id=f.id, version=1,
                      formula_expr="mean(close,5)", direction="higher_better")
    db.add(v)
    db.flush()
    return f, v


def _make_panels(n_days: int = 340, n_symbols: int = 20, seed: int = 10):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-05", periods=n_days, freq="B")
    symbols = [f"{i:06d}" for i in range(n_symbols)]
    fv = pd.DataFrame(
        rng.normal(0, 1, size=(n_days, n_symbols)),
        index=dates, columns=symbols)
    fr = fv.shift(-5) * 0.01 + rng.normal(0, 0.01, size=(n_days, n_symbols))
    return fv, fr


# ══════════════════════════════════════════════════════════
# 1) stats 写入钩子（run_evaluation，缺省不变）
# ══════════════════════════════════════════════════════════


class TestStatsWriteHook:
    def test_provider_merges_stats_into_metrics(self, db_session):
        f, v = _make_factor_and_version(db_session)
        db_session.commit()
        fv, fr = _make_panels()

        def _provider(*, ic_series, **kw):  # noqa: ARG001
            return {"stats": {"p_value": 0.03}, "icir_raw": kw["icir"],
                    "icir_adjusted": kw["icir"]}

        outcome = run_evaluation(
            db_session, factor_version_id=v.id,
            factor_values=fv, forward_returns=fr,
            config=EvaluationConfig(factor_kind="continuous", target_horizon=5,
                                    n_groups=5),
            data_cutoff_at=datetime(2026, 7, 24),
            stats_provider=_provider,
        )
        db_session.commit()
        run = get_evaluation_run(db_session, outcome.run_id)
        metrics = json.loads(run.metrics_json)
        assert metrics["stats"]["p_value"] == 0.03
        assert metrics["icir_raw"] == metrics["icir_adjusted"]
        assert metrics["icir_raw"] == outcome.metrics["icir_raw"]

    def test_without_provider_no_stats_key(self, db_session):
        f, v = _make_factor_and_version(db_session)
        db_session.commit()
        fv, fr = _make_panels(seed=12)
        outcome = run_evaluation(
            db_session, factor_version_id=v.id,
            factor_values=fv, forward_returns=fr,
            config=EvaluationConfig(factor_kind="continuous"),
            data_cutoff_at=datetime(2026, 7, 24),
        )
        db_session.commit()
        assert "stats" not in outcome.metrics


# ══════════════════════════════════════════════════════════
# 2) 挖掘域真实统计层闭包（compute_stats 8 方法）
# ══════════════════════════════════════════════════════════


class TestMiningStatsProvider:
    def test_provider_computes_8_method_stats(self, db_session):
        from types import SimpleNamespace

        from app.services.factors.mining.evaluation_adapter import _stats_provider_for

        run = FactorMiningRun(
            id="run-b2", status="succeeded", candidate_pool_snapshot_id="snap-b2",
            data_cutoff_at=datetime(2026, 12, 1), start_date=datetime(2026, 1, 5),
            end_date=datetime(2026, 11, 1), rebalance_frequency="daily",
            split_method="ratio", split_algorithm_version="split-1.0.0",
            target_horizon=5, total_trials=120, random_seed=7,
        )
        db_session.add(run)
        db_session.commit()

        ctx = SimpleNamespace(run_id=run.id, rebalance_frequency="daily")
        provider = _stats_provider_for(db_session, ctx, train_icir=0.1)
        ic_series = [0.05 + 0.001 * i for i in range(40)]
        rng = np.random.default_rng(3)
        panels = rng.normal(0, 1, size=(40, 8))
        out = provider(ic_series=ic_series, icir=0.3,
                       factor_panel=panels, return_panel=panels)
        stats = out["stats"]
        for key in ("p_value", "p_adj_bonferroni", "q_value_fdr", "ci_lower",
                    "ci_upper", "perm_p_value", "dsr_icir", "decay_ratio",
                    "walk_forward", "degraded", "total_trials"):
            assert key in stats, key
        assert stats["total_trials"] == 120
        assert abs(stats["decay_ratio"] - 3.0) < 1e-9   # 0.3 / 0.1
        assert out["icir_raw"] == 0.3 == out["icir_adjusted"]


# ══════════════════════════════════════════════════════════
# 3) evaluations 契约：get_mining_evaluation 返回 stats
# ══════════════════════════════════════════════════════════


class TestEvaluationContract:
    def test_get_mining_evaluation_returns_stats(self, db_session):
        f, v = _make_factor_and_version(db_session)
        run = EvaluationRun(
            id="eval-b2", factor_version_id=v.id,
            metrics_json=json.dumps({
                "ic": {"icir": 0.4},
                "stats": {"p_value": 0.02, "ci_lower": 0.05},
            }),
            gate_result="passed",
            data_cutoff_at=datetime(2026, 8, 1),
        )
        db_session.add(run)
        db_session.commit()

        got = SVC.get_mining_evaluation(db_session, "eval-b2")
        assert got is not None
        assert got["stats"]["p_value"] == 0.02
        assert got["metrics"]["ic"]["icir"] == 0.4


# ══════════════════════════════════════════════════════════
# 4) 分级落库 + D 级移出 FactorSet（finalize 后消费真实数据）
# ══════════════════════════════════════════════════════════


def _split_ctx(db_session, run: FactorMiningRun, warehouse_path=None):
    """构建 finalize_run 所需 MiningContext（与 G4 E2E 同款；无仓库也可用）。"""
    from app.services.factors.mining.contracts import MiningContext
    from app.services.factors.mining.evaluation_adapter import build_split

    dates = [date(2026, 1, 5) + timedelta(days=i) for i in range(400)]
    return MiningContext(
        run_id=run.id, candidate_pool_snapshot_id=str(run.candidate_pool_snapshot_id),
        data_cutoff_at=run.data_cutoff_at, start_date=date(2026, 1, 5),
        end_date=date(2026, 11, 1), rebalance_frequency="daily",
        target_horizon=5,
        split=build_split(all_dates=dates, frequency="daily", target_horizon=5),
        purge_points=1, embargo_points=1, train_ratio=0.6,
        validation_ratio=0.2, random_seed=42, config_hash="cfg-b2",
        split_algorithm_version="split-1.0.0",
        target_calc_batch_id="mining-b2",
        warehouse_path=warehouse_path or ":memory:",
    )


class TestGradingAfterFinalize:
    def test_finalize_grades_and_governs_d_grade(self, db_session):
        f, v = _make_factor_and_version(db_session)
        run = FactorMiningRun(
            id="run-b2g", status="running", candidate_pool_snapshot_id="snap-b2g",
            data_cutoff_at=datetime(2026, 12, 1), start_date=datetime(2026, 1, 5),
            end_date=datetime(2026, 11, 1), rebalance_frequency="daily",
            split_method="ratio", split_algorithm_version="split-1.0.0",
            target_horizon=5, random_seed=7,
        )
        db_session.add(run)
        db_session.flush()
        cand = FactorMiningCandidate(
            id="cand-b2g", run_id=run.id,
            formula_expr="mean(close,5)", canonical_formula="mean(close,5)",
            formula_hash="d" * 31 + "0", generation_icir=0.02,
            generation_complexity=3, operation="elite",
            expected_direction="positive",
        )
        db_session.add(cand)
        # 先在 FactorSet 放入该因子（验证 D 级会被移出）
        v.validation_status = "valid"
        db_session.flush()
        fset = FSS.create_factor_set(
            db_session, payload=FactorSetCreate(name="B2 精选")).factor_set
        db_session.flush()
        FSS.add_member(
            db_session, factor_set_id=fset.id,
            payload=FactorSetMemberCreate(factor_id=f.id,
                                          factor_version_id=v.id,
                                          factor_code=f.code))
        db_session.commit()

        @dataclass
        class _Outcome:
            run_id: str
            gate_result: str
            rejection_reasons: list
            metrics: dict
            time_split: object
            coverage: object

        @dataclass
        class _Cov:
            pass

        def _fake_draft_creator(db, *, digest, formula, category=None,
                                direction="positive", logic="", created_by="mining",
                                frequency=None):
            from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate
            from app.services.factors import factor_registry as FR
            return v.id, f.code  # 复用既有 factor/version

        def _fake_final_runner(db, *, factor_version_id, factor_values, forward_returns,
                               config, created_by="local_user", **kw):  # noqa: ARG001
            return _Outcome(run_id=f"eval-{factor_version_id}",
                            gate_result="passed", rejection_reasons=[],
                            metrics={"ic": {"icir": 0.02}},
                            time_split=None, coverage=_Cov())

        ctx = _split_ctx(db_session, run)
        final = SVC.finalize_run(
            db_session, ctx=ctx, run_id=run.id, top_k=1,
            draft_creator=_fake_draft_creator,
            run_evaluator=_fake_final_runner)
        assert final["status"] == "succeeded"

        # B2：分级已落库（factor_versions 当前态 4 列）
        db_session.refresh(v)
        assert v.quality_grade in ("S", "A", "B", "C", "D")
        assert v.grade_updated_at is not None

        # D（弱 ICIR=0.02）→ 已移出 FactorSet
        members = db_session.query(FactorSetMember).filter_by(
            factor_id=f.id).all()
        assert len(members) == 0, [m.factor_id for m in members]


# ══════════════════════════════════════════════════════════
# 5) 季度重评消费真实列（grade_manual_adjusted 以 factor_versions 为准）
# ══════════════════════════════════════════════════════════


class TestQuarterlyConsumesRealColumn:
    def test_quarterly_skips_manual_from_real_column(self, db_session):
        from app.services.factors.mining import factor_grading as FG

        f, v = _make_factor_and_version(db_session)
        db_session.add(FactorGradeHistory(
            id="vq-1", factor_version_id=v.id, grade="B",
            reason="季度评定", metrics_snapshot_json="{}",
            source="quarterly", manual_adjusted=0,
            created_at=datetime(2026, 6, 1),
        ))
        v.quality_grade = "B"
        v.grade_manual_adjusted = 1          # 真实列：人工调整过
        db_session.commit()

        stats = FG.run_quarterly_review(db_session, actor="system")
        assert stats["skipped_manual"] >= 1
        db_session.refresh(v)
        assert v.quality_grade == "B"         # 未被季度任务覆盖