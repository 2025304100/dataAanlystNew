"""Stage-3 contract tests for the automatic-simulation public entries."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.portfolio import Portfolio


def _portfolio(db_session) -> Portfolio:
    portfolio = Portfolio(
        name="stage3-auto-direct-entry",
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


def _applied_snapshot(db_session, portfolio_id: int) -> StrategyExecutionSnapshot:
    snapshot = StrategyExecutionSnapshot(
        id="stage3-auto-direct-applied",
        snapshot_no=1,
        portfolio_id=portfolio_id,
        decision_clock_json="{}",
        member_snapshot_json="[]",
        snapshot_type="save_and_apply",
        snapshot_hash="stage3-auto-direct-hash",
        effective_from=datetime(2026, 8, 21, 7, 0),
        created_by="stage3-test",
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


@pytest.mark.parametrize("runner_name", ["execute_member_source", "run_idempotent_member_source"])
def test_direct_auto_entry_resolves_applied_snapshot_and_uses_decision_engine(
    db_session, monkeypatch, runner_name,
):
    """Both exported automatic entries derive decisions exclusively from a snapshot plan set."""
    from app.services import decision_engine
    from app.services import auto_trade_member_source as member_source

    portfolio = _portfolio(db_session)
    snapshot = _applied_snapshot(db_session, portfolio.id)
    observed: dict[str, object] = {}

    def fake_evaluate(_db, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(decision_run_id="stage3-auto-direct-run", order_plans=[])

    monkeypatch.setattr(decision_engine.default_engine, "evaluate", fake_evaluate)
    runner = getattr(member_source, runner_name)

    result = runner(db_session, portfolio_id=portfolio.id, dry_run=True)

    assert observed["strategy_snapshot_id"] == snapshot.id
    assert observed["run_type"] == "auto_simulation"
    assert observed["trade_date"] is not None
    assert result["strategy_snapshot_id"] == snapshot.id
    assert result["decision_run_id"] == "stage3-auto-direct-run"


@pytest.mark.parametrize("runner_name", ["execute_member_source", "run_idempotent_member_source"])
def test_direct_auto_entry_never_falls_back_to_legacy_member_rules_without_snapshot(
    db_session, monkeypatch, runner_name,
):
    """No applied snapshot is a fail-closed error, not permission to call decide_trades."""
    from app.services import auto_trade_member_source as member_source

    portfolio = _portfolio(db_session)
    monkeypatch.setattr(
        member_source,
        "decide_trades",
        lambda *_args, **_kwargs: pytest.fail("legacy decide_trades must not run"),
    )
    runner = getattr(member_source, runner_name)

    with pytest.raises(ValueError, match="STRATEGY_SNAPSHOT_REQUIRED"):
        runner(db_session, portfolio_id=portfolio.id, dry_run=True)


def test_auto_trade_response_schema_preserves_resolved_snapshot_id():
    """The public execute response cannot silently drop the immutable snapshot ID."""
    from app.schemas.portfolio import AutoTradeResult

    response = AutoTradeResult.model_validate({
        "portfolio_id": 42,
        "dry_run": True,
        "sells": [],
        "buys": [],
        "errors": [],
        "executed_at": "2026-08-21T20:30:00+00:00",
        "strategy_snapshot_id": "stage3-auto-response-snapshot",
    })

    assert response.strategy_snapshot_id == "stage3-auto-response-snapshot"


def test_real_dual_run_never_executes_legacy_source_when_member_source_is_disabled(
    db_session, monkeypatch,
):
    """A disabled new-source switch blocks real execution instead of using old scan rules."""
    from app.services import auto_trade_task
    from app.services.auto_trade_dual_run import (
        AutoTradeNotReadyError,
        TradeSet,
        run_dual_trade,
    )

    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "false")
    portfolio = _portfolio(db_session)
    _applied_snapshot(db_session, portfolio.id)
    legacy_execution_calls: list[dict] = []

    monkeypatch.setattr(
        "app.services.auto_trade_dual_run.capture_trade_set_from_old_logic",
        lambda *_args, **_kwargs: TradeSet(),
    )
    monkeypatch.setattr(
        "app.services.auto_trade_dual_run.capture_trade_set_from_new_logic",
        lambda *_args, **_kwargs: TradeSet(),
    )
    monkeypatch.setattr(
        auto_trade_task,
        "run_auto_trade",
        lambda *_args, **kwargs: legacy_execution_calls.append(kwargs) or {
            "sells": [], "buys": [], "errors": [],
        },
    )

    with pytest.raises(AutoTradeNotReadyError) as error:
        run_dual_trade(
            db_session,
            portfolio_id=portfolio.id,
            dry_run=False,
            require_readiness=False,
            save_diff=False,
        )

    assert "DECISION_ENGINE_SOURCE_REQUIRED" in {
        blocker.code for blocker in error.value.blockers
    }
    assert legacy_execution_calls == []
