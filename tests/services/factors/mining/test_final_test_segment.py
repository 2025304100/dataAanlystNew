"""Mining final evaluation measures the held-out test period, not validation."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from app.services.factors.factor_evaluator import TimeSplit
from app.services.factors.mining.contracts import MiningContext
from app.services.factors.mining.evaluation_adapter import evaluate_full

pytestmark = pytest.mark.whitebox


def test_final_test_ic_uses_independent_held_out_returns(db_session):
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(300)]
    split = TimeSplit(train_start=days[0], train_end=days[179],
                      validation_start=days[185], validation_end=days[239],
                      test_start=days[245], test_end=days[-1])
    ctx = MiningContext(run_id="test-segment", candidate_pool_snapshot_id="test-pool",
                        data_cutoff_at=datetime(2026, 1, 1), start_date=days[0], end_date=days[-1],
                        rebalance_frequency="daily", target_horizon=5, split=split,
                        purge_points=5, embargo_points=5, train_ratio=0.6, validation_ratio=0.2,
                        random_seed=0, config_hash="held-out-test")
    factors = pd.DataFrame([[1.0, 2.0, 3.0, 4.0, 5.0] for _ in days], index=days, columns=["A", "B", "C", "D", "E"])
    # Validation ranks are the exact opposite of the test ranks.
    returns = pd.DataFrame([([-0.01, -0.02, -0.03, -0.04, -0.05] if day < days[245]
                            else [0.01, 0.02, 0.03, 0.04, 0.05]) for day in days], index=days, columns=factors.columns)
    result = evaluate_full(ctx, individual={"formula": "close", "formula_hash": "f" * 32},
                           db=db_session, factor_values=factors, forward_returns=returns)
    assert result.metrics["ic"]["rank_ic_mean"] == pytest.approx(1.0)
    assert result.metrics["evaluation_segment"] == "test"
    assert result.metrics["evaluation_start"] == "2025-09-03"
    assert result.metrics["evaluation_end"] == "2025-10-27"
