"""Public final evaluation compares other factors, never the candidate's own factor."""
from datetime import datetime

import pandas as pd
import pytest

from app.models.factor import Factor
from app.models.factor_mining import FactorMiningRun
from app.models.factor_model import FactorVersion
from app.services.factors.mining.contracts import MiningContext
from app.services.factors.mining.evaluation_adapter import (
    build_split, create_candidate_draft, evaluate_full,
)
from app.services.factors.store import FactorWarehouse
from test_m1_closure import warehouse_path

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("with_other_factor", [False, True])
def test_final_evaluation_excludes_self_but_keeps_other_factor_same_formula(
        db_session, warehouse_path, with_other_factor):
    db = db_session
    digest = "8b32d6ef7b64d6ef8b32d6ef7b64d6ef"
    version_id, code = create_candidate_draft(db, digest=digest, formula="close",
        category="trend", direction="positive", logic="同一已启用因子的再发现",
        created_by="isolated_test", frequency="daily")
    version = db.get(FactorVersion, version_id)
    own_factor = db.get(Factor, version.factor_id)
    own_factor.status = "active"
    own_factor.lifecycle_status = "active"
    own_factor.is_active = 1
    own_factor.active_version_id = version_id
    other_version_id = None
    if with_other_factor:
        other = Factor(code="different_factor_same_formula", name="Other factor",
            category="trend", direction="positive", status="active", lifecycle_status="active", is_active=1)
        db.add(other)
        db.flush()
        other_version = FactorVersion(factor_id=other.id, version=1,
            formula_expr="close", validation_status="valid")
        db.add(other_version)
        db.flush()
        other.active_version_id = other_version.id
        other_version_id = other_version.id
    run = FactorMiningRun(id="self-reference-run", status="running",
        candidate_pool_snapshot_id="self-reference-snapshot", data_cutoff_at=datetime(2027, 3, 1),
        start_date=datetime(2026, 1, 5), end_date=datetime(2027, 2, 1),
        rebalance_frequency="daily", target_horizon=5, total_trials=1)
    db.add(run)
    db.commit()
    targets, _, _ = FactorWarehouse(warehouse_path).get_target_panel("a5-target", "target_5d_return",
        start_date=run.start_date.date(), end_date=run.end_date.date(),
        as_of_exit_date=run.data_cutoff_at.date())
    dates = sorted(set(pd.to_datetime(targets["signal_date"]).dt.date))
    split, budget = build_split(all_dates=dates, frequency="daily", target_horizon=5, tail_loss_points=0)
    ctx = MiningContext(run_id=run.id, candidate_pool_snapshot_id="self-reference-snapshot",
        data_cutoff_at=run.data_cutoff_at, start_date=run.start_date.date(), end_date=run.end_date.date(),
        rebalance_frequency="daily", target_horizon=5, split=split,
        purge_points=budget.purge_points, embargo_points=budget.embargo_points,
        train_ratio=.6, validation_ratio=.2, random_seed=42, config_hash="self-reference",
        split_algorithm_version="split-1.0.0", target_calc_batch_id="a5-target",
        warehouse_path=warehouse_path, candidate_symbols=tuple(sorted(targets["symbol"].unique())),
        signal_dates=tuple(dates))
    outcome = evaluate_full(ctx, individual={"formula": "close", "canonical_formula": "close",
        "formula_hash": digest}, db=db, factor_values=None, forward_returns=None, train_icir=.3)
    assert outcome.factor_version_id == version_id
    evidence = outcome.metrics["novelty"]
    assert code not in evidence["reference_codes"]
    assert evidence["excluded_self_code"] == code
    if with_other_factor:
        assert evidence["reference_version_ids"] == [other_version_id]
        assert evidence["status"] == "complete"
        assert outcome.metrics["correlation"] == pytest.approx(1.0)
    else:
        assert evidence["reference_count"] == 0
        assert evidence["status"] == "empty_library"
        assert outcome.metrics["correlation"] == 0.0
