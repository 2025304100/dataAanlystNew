"""Backtest history detail must remain readable for persisted snapshots."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from app.api.routes.backtest import get_backtest_run
from app.models.backtest import BacktestRun
from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol


pytestmark = pytest.mark.whitebox


def test_backtest_detail_uses_run_pit_mode_when_snapshot_has_no_pit_column(db_session):
    """Historical detail must not dereference a non-existent snapshot attribute."""
    portfolio = Portfolio(
        name="Snapshot Detail Contract",
        account_type="simulated",
        asset_scope="stock",
        total_capital=100_000,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
    )
    symbol = Symbol(
        symbol="SNAP_DETAIL_01",
        name="Snapshot Detail Symbol",
        asset_type="stock",
        market="sz",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()

    snapshot = StrategyExecutionSnapshot(
        id="snapshot-detail-contract",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        decision_clock_json=json.dumps({}),
        member_snapshot_json=json.dumps([]),
        snapshot_type="save_and_apply",
        snapshot_hash="snapshot-detail-hash",
        versions_json=json.dumps({}),
        effective_from=datetime(2026, 8, 20, 15, 5),
    )
    db_session.add(snapshot)
    db_session.flush()

    run = BacktestRun(
        portfolio_id=portfolio.id,
        run_name="snapshot-detail-run",
        symbols_json=json.dumps([symbol.id]),
        rule_config_json=json.dumps({}),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 20),
        initial_capital=100_000,
        status="completed",
        strategy_snapshot_id=snapshot.id,
        pit_mode="strict_pit_safe",
    )
    db_session.add(run)
    db_session.flush()

    detail = get_backtest_run(run.id, db=db_session)

    assert detail.decision_snapshot is not None
    assert detail.decision_snapshot["pit_mode"] == "strict_pit_safe"
