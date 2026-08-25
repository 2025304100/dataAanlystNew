"""Stage-3 contract: automatic simulation must start from an applied snapshot."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.portfolio import Portfolio


def _portfolio(db_session) -> Portfolio:
    portfolio = Portfolio(
        name="stage3-auto-snapshot-entry",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.2,
        auto_trade_enabled=1,
    )
    db_session.add(portfolio)
    db_session.flush()
    return portfolio


def _snapshot(
    db_session,
    *,
    portfolio_id: int,
    snapshot_id: str,
    snapshot_no: int,
    snapshot_type: str = "save_and_apply",
) -> StrategyExecutionSnapshot:
    snapshot = StrategyExecutionSnapshot(
        id=snapshot_id,
        snapshot_no=snapshot_no,
        portfolio_id=portfolio_id,
        decision_clock_json="{}",
        member_snapshot_json="[]",
        snapshot_type=snapshot_type,
        snapshot_hash=f"hash-{snapshot_id}",
        effective_from=datetime(2026, 8, 21, 7, 0),
        created_by="stage3-test",
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def test_auto_simulation_resolves_the_latest_applied_snapshot(db_session):
    """A run without an explicit id starts from the latest applied snapshot."""
    from app.services.auto_trade_dual_run import resolve_applied_snapshot_id

    portfolio = _portfolio(db_session)
    _snapshot(
        db_session,
        portfolio_id=portfolio.id,
        snapshot_id="stage3-auto-snapshot-old",
        snapshot_no=1,
    )
    latest = _snapshot(
        db_session,
        portfolio_id=portfolio.id,
        snapshot_id="stage3-auto-snapshot-latest",
        snapshot_no=2,
    )

    assert resolve_applied_snapshot_id(db_session, portfolio_id=portfolio.id) == latest.id


def test_new_auto_simulation_entry_passes_latest_snapshot_to_decision_engine(
    db_session, monkeypatch,
):
    """Manual dry-runs use the same applied snapshot contract as scheduled runs."""
    from app.services.auto_trade_dual_run import TradeSet, run_dual_trade

    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    portfolio = _portfolio(db_session)
    latest = _snapshot(
        db_session,
        portfolio_id=portfolio.id,
        snapshot_id="stage3-auto-entry-latest",
        snapshot_no=1,
    )
    observed: dict[str, object] = {}

    def fake_execute(_db, **kwargs):
        observed.update(kwargs)
        return {
            "portfolio_id": portfolio.id,
            "buy_decisions": [],
            "sell_decisions": [],
            "signal_decisions": [],
            "rejected_decisions": [],
            "executed_orders": [],
            "pending_orders": [],
            "skipped_orders": [],
            "errors": [],
        }

    with patch(
        "app.services.auto_trade_dual_run.capture_trade_set_from_old_logic",
        return_value=TradeSet(),
    ), patch(
        "app.services.auto_trade_dual_run.capture_trade_set_from_new_logic",
        return_value=TradeSet(),
    ), patch(
        "app.services.auto_trade_member_source.run_idempotent_member_source",
        side_effect=fake_execute,
    ):
        run_dual_trade(
            db_session,
            portfolio_id=portfolio.id,
            dry_run=True,
            save_diff=False,
        )

    assert observed["strategy_snapshot_id"] == latest.id
    assert observed["trade_date"] is not None


def test_new_auto_simulation_fails_closed_without_an_applied_snapshot(
    db_session, monkeypatch,
):
    """No snapshot must never fall back to the legacy member-side order path."""
    import pytest

    from app.services.auto_trade_dual_run import (
        AutoTradeNotReadyError,
        run_dual_trade,
    )

    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    portfolio = _portfolio(db_session)

    with pytest.raises(AutoTradeNotReadyError) as error:
        run_dual_trade(
            db_session,
            portfolio_id=portfolio.id,
            dry_run=False,
            require_readiness=False,
        )

    assert "STRATEGY_SNAPSHOT_REQUIRED" in {
        item.code for item in error.value.blockers
    }
