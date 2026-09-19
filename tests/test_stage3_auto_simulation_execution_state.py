"""Stage-3 auto-simulation execution state at the public snapshot entry point."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.decision_engine import (
    DecisionEvidence,
    DecisionRun,
    StrategyExecutionSnapshot,
)
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import execute_member_source
from app.services.sim_accounts import cash_balance


TRADE_DATE = date(2026, 2, 3)
EXECUTION_DATE = date(2026, 2, 4)


def _seed_snapshot_plan(db_session, *, total_capital: float, volume_limit_pct: float | None):
    portfolio = Portfolio(
        name=f"stage3-auto-state-{total_capital}-{volume_limit_pct}",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=total_capital,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=1.0,
        auto_trade_enabled=1,
    )
    symbol = Symbol(
        symbol=f"60{int(total_capital):04d}",
        name="Auto Execution State",
        asset_type="stock",
        market="SH",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()

    cost_config = {
        "commission_rate": 0.0003,
        "min_commission": 0.0,
        "stamp_tax_rate": 0.0,
        "transfer_fee_rate": 0.0,
        "slippage_buy_bps": 0,
        "slippage_sell_bps": 0,
    }
    if volume_limit_pct is not None:
        cost_config["volume_limit_pct"] = volume_limit_pct

    snapshot_id = f"snapshot-auto-state-{int(total_capital)}"
    snapshot = StrategyExecutionSnapshot(
        id=snapshot_id,
        snapshot_no=1,
        portfolio_id=portfolio.id,
        decision_clock_json=json.dumps({"decision_at": "20:30 Asia/Shanghai"}),
        cost_config_json=json.dumps(cost_config),
        member_snapshot_json=json.dumps({"members": []}),
        snapshot_hash=hashlib.sha256(snapshot_id.encode()).hexdigest(),
        effective_from=datetime(2026, 2, 3, 20, 30),
        created_by="test",
    )
    db_session.add(snapshot)
    db_session.flush()

    run_id = f"run-auto-state-{int(total_capital)}"
    decision_run = DecisionRun(
        id=run_id,
        strategy_snapshot_id=snapshot_id,
        portfolio_id=portfolio.id,
        run_type="auto_simulation",
        status="SUCCEEDED",
        trade_date=TRADE_DATE,
        decision_at=datetime(2026, 2, 3, 20, 30),
        data_cutoff_at=datetime(2026, 2, 3, 20, 0),
        execution_at=datetime(2026, 2, 4, 9, 30),
    )
    db_session.add(decision_run)
    db_session.flush()
    evidence_id = f"evidence-auto-state-{int(total_capital)}"
    evidence = DecisionEvidence(
        id=evidence_id,
        decision_run_id=run_id,
        strategy_snapshot_id=snapshot_id,
        portfolio_id=portfolio.id,
        symbol_id=symbol.id,
        trade_date=TRADE_DATE,
        decision_at=datetime(2026, 2, 3, 20, 30),
        data_cutoff_at=datetime(2026, 2, 3, 20, 0),
        execution_at=datetime(2026, 2, 4, 9, 30),
        action="BUY",
        min_lot_size=100,
        target_quantity=200.0,
        target_qty_delta=200.0,
        intended_price=10.0,
        content_hash=hashlib.sha256(evidence_id.encode()).hexdigest(),
    )
    db_session.add_all([
        evidence,
        DailyBar(
            symbol_id=symbol.id,
            trade_date=EXECUTION_DATE,
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            volume=300.0,
        ),
    ])
    db_session.commit()

    plan = SimpleNamespace(
        symbol_id=symbol.id,
        action="BUY",
        target_quantity=200.0,
        intended_price=10.0,
        reason_code="SIGNAL_OPEN",
        evidence_id=evidence_id,
        order_plan_id=f"plan-auto-state-{int(total_capital)}",
        signal_date=TRADE_DATE,
        execution_date=EXECUTION_DATE,
    )
    return portfolio, symbol, snapshot_id, evidence_id, plan


def _stub_snapshot_evaluation(monkeypatch, plan):
    from app.services import decision_engine

    monkeypatch.setattr(
        decision_engine.default_engine,
        "evaluate",
        lambda *_args, **_kwargs: SimpleNamespace(
            decision_run_id="run-auto-state-stub",
            order_plans=[plan],
        ),
    )


def test_snapshot_partial_fill_records_filled_leg_and_pending_remainder(db_session, monkeypatch):
    portfolio, _symbol, snapshot_id, evidence_id, plan = _seed_snapshot_plan(
        db_session,
        total_capital=50_000.0,
        volume_limit_pct=0.5,
    )
    _stub_snapshot_evaluation(monkeypatch, plan)

    result = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot_id,
        trade_date=TRADE_DATE,
        dry_run=False,
    )

    order = db_session.execute(
        select(SimOrder).where(SimOrder.client_order_key == plan.order_plan_id)
    ).scalar_one()
    trade = db_session.execute(select(SimTrade).where(SimTrade.order_id == order.id)).scalar_one()
    evidence = db_session.get(DecisionEvidence, evidence_id)
    position = db_session.execute(
        select(Position).where(
            Position.portfolio_id == portfolio.id,
            Position.symbol_id == plan.symbol_id,
        )
    ).scalar_one()

    assert order.status == "partial"
    assert order.quantity == 200.0
    assert order.filled_quantity == 100.0
    assert order.decision_evidence_id == evidence_id
    assert trade.quantity == 100.0
    assert position.quantity == 100.0
    assert cash_balance(db_session, portfolio.id) == 48_999.7

    versions = json.loads(evidence.versions_json)
    trace = json.loads(evidence.rejections_trace_json)
    assert evidence.rejection_reason == "PARTIAL_FILL_PENDING"
    assert versions["order_plan_status"] == "PARTIAL_FILL_PENDING"
    assert versions["order_plan_remaining_quantity"] == 100.0
    assert versions["order_plan_next_retry_status"] == "PENDING_RETRY"
    assert trace[-1]["reason"] == "PARTIAL_FILL"
    assert trace[-1]["remaining_quantity"] == 100.0
    assert result["executed_orders"][0]["status"] == "partial"
    assert result["pending_orders"][0]["remaining_quantity"] == 100.0


def test_admin_pause_stops_new_buys_and_keeps_auditable_rejection(db_session, monkeypatch):
    """G6: manual stop blocks new entries before matching/account writes."""
    portfolio, _symbol, snapshot_id, evidence_id, plan = _seed_snapshot_plan(
        db_session,
        total_capital=50_000.0,
        volume_limit_pct=None,
    )
    _stub_snapshot_evaluation(monkeypatch, plan)
    from app.services.portfolio_state_machine import transition_portfolio_state

    transition_portfolio_state(
        db_session,
        portfolio.id,
        "ADMIN_PAUSED",
        operator_id="g6-test",
        reason="manual stop new buys",
    )
    db_session.commit()

    result = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot_id,
        trade_date=TRADE_DATE,
        dry_run=False,
    )

    order = db_session.execute(
        select(SimOrder).where(SimOrder.client_order_key == plan.order_plan_id)
    ).scalar_one()
    evidence = db_session.get(DecisionEvidence, evidence_id)
    assert order.status == "rejected"
    assert order.rejection_code == "PORTFOLIO_STATE_NEW_BUY_BLOCKED"
    assert order.decision_evidence_id == evidence_id
    assert db_session.execute(select(SimTrade).where(SimTrade.order_id == order.id)).scalar_one_or_none() is None
    assert evidence.rejection_reason == "PORTFOLIO_STATE_NEW_BUY_BLOCKED"
    assert result["executed_orders"] == []
    assert result["rejected_decisions"][0]["rejection_code"] == "PORTFOLIO_STATE_NEW_BUY_BLOCKED"


def test_snapshot_insufficient_cash_records_rejected_order_and_evidence(db_session, monkeypatch):
    portfolio, _symbol, snapshot_id, evidence_id, plan = _seed_snapshot_plan(
        db_session,
        total_capital=1_000.0,
        volume_limit_pct=None,
    )
    _stub_snapshot_evaluation(monkeypatch, plan)

    result = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot_id,
        trade_date=TRADE_DATE,
        dry_run=False,
    )

    order = db_session.execute(
        select(SimOrder).where(SimOrder.client_order_key == plan.order_plan_id)
    ).scalar_one()
    evidence = db_session.get(DecisionEvidence, evidence_id)
    trade_count = db_session.execute(select(SimTrade).where(SimTrade.order_id == order.id)).scalars().all()

    assert order.status == "rejected"
    assert order.quantity == 200.0
    assert order.filled_quantity == 0.0
    assert order.decision_evidence_id == evidence_id
    assert order.rejection_code == "INSUFFICIENT_CASH"
    assert trade_count == []
    assert cash_balance(db_session, portfolio.id) == 1_000.0

    versions = json.loads(evidence.versions_json)
    trace = json.loads(evidence.rejections_trace_json)
    assert evidence.rejection_reason == "INSUFFICIENT_CASH"
    assert versions["order_plan_status"] == "REJECTED_INSUFFICIENT_CASH"
    assert trace[-1]["reason"] == "INSUFFICIENT_CASH"
    assert result["errors"] == []
    assert result["rejected_decisions"][0]["rejection_code"] == "INSUFFICIENT_CASH"


def test_snapshot_partial_match_with_insufficient_cash_is_audited_not_raised(db_session, monkeypatch):
    portfolio, _symbol, snapshot_id, evidence_id, plan = _seed_snapshot_plan(
        db_session,
        total_capital=1_000.0,
        volume_limit_pct=0.5,
    )
    _stub_snapshot_evaluation(monkeypatch, plan)

    result = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot_id,
        trade_date=TRADE_DATE,
        dry_run=False,
    )

    order = db_session.execute(
        select(SimOrder).where(SimOrder.client_order_key == plan.order_plan_id)
    ).scalar_one()
    evidence = db_session.get(DecisionEvidence, evidence_id)

    assert order.status == "rejected"
    assert order.filled_quantity == 0.0
    assert evidence.rejection_reason == "INSUFFICIENT_CASH"
    assert result["errors"] == []


def test_snapshot_partial_fill_is_retried_on_next_session_without_duplicate_fill(
    db_session, monkeypatch,
):
    """A scheduled rerun consumes the persisted remainder exactly once."""
    portfolio, symbol, snapshot_id, evidence_id, plan = _seed_snapshot_plan(
        db_session,
        total_capital=60_000.0,
        volume_limit_pct=0.5,
    )
    from app.services import decision_engine

    monkeypatch.setattr(
        decision_engine.default_engine,
        "evaluate",
        lambda *_args, **_kwargs: SimpleNamespace(
            decision_run_id="run-auto-state-first",
            order_plans=[plan],
        ),
    )
    first = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot_id,
        trade_date=TRADE_DATE,
        dry_run=False,
    )
    assert first["pending_orders"][0]["remaining_quantity"] == 100.0

    # The next session has enough volume for the persisted remainder.
    db_session.add(
        DailyBar(
            symbol_id=symbol.id,
            trade_date=date(2026, 2, 5),
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            volume=1_000.0,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        decision_engine.default_engine,
        "evaluate",
        lambda *_args, **_kwargs: SimpleNamespace(
            decision_run_id="run-auto-state-retry",
            order_plans=[],
        ),
    )

    second = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot_id,
        # The scheduler's next decision cycle is the day after the original
        # execution; the adapter should select the next available bar.
        trade_date=EXECUTION_DATE,
        dry_run=False,
    )
    order = db_session.execute(
        select(SimOrder).where(SimOrder.client_order_key == plan.order_plan_id)
    ).scalar_one()
    trades = db_session.execute(
        select(SimTrade).where(SimTrade.order_id == order.id).order_by(SimTrade.id)
    ).scalars().all()
    evidence = db_session.get(DecisionEvidence, evidence_id)
    position = db_session.execute(
        select(Position).where(
            Position.portfolio_id == portfolio.id,
            Position.symbol_id == symbol.id,
        )
    ).scalar_one()

    assert second["pending_orders"] == []
    assert order.status == "filled"
    assert order.filled_quantity == 200.0
    assert len(trades) == 2
    assert [trade.quantity for trade in trades] == [100.0, 100.0]
    assert position.quantity == 200.0
    assert cash_balance(db_session, portfolio.id) == 57_999.4
    versions = json.loads(evidence.versions_json)
    trace = json.loads(evidence.rejections_trace_json)
    assert versions["order_plan_status"] == "FILLED"
    assert versions["order_plan_remaining_quantity"] == 0.0
    assert any(item.get("reason") == "PARTIAL_FILL_RETRY" for item in trace)

    # Replaying the same scheduler cycle is idempotent: no third trade and no
    # extra cash movement for the already-consumed remainder.
    third = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot_id,
        trade_date=EXECUTION_DATE,
        dry_run=False,
    )
    assert third["pending_orders"] == []
    assert len(db_session.execute(
        select(SimTrade).where(SimTrade.order_id == order.id)
    ).scalars().all()) == 2
    assert cash_balance(db_session, portfolio.id) == 57_999.4
