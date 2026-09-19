"""Stage-5 reconciliation must read the real auto-simulation execution chain.

The public seam is ``reconcile_trade_date``.  Its default path must reconcile
the immutable DecisionEvidence with its exact SimOrder/SimTrade execution
records; callers must not have to supply an in-memory trades provider merely
to obtain a meaningful result.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from app.models.decision_engine import (
    DecisionEvidence,
    DecisionRun,
    StrategyExecutionSnapshot,
)
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.portfolio_reconciliation import reconcile_trade_date
from app.services.portfolio_state_machine import _get_status


TRADE_DATE = date(2026, 8, 20)


def _seed_partial_auto_execution(db_session):
    portfolio = Portfolio(
        name="stage5-reconciliation-execution",
        account_type="simulated",
        asset_scope="stock",
        total_capital=100_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
    )
    symbol = Symbol(
        symbol="STAGE5REC",
        name="Stage 5 Reconciliation",
        asset_type="stock",
        market="SH",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()

    snapshot = StrategyExecutionSnapshot(
        id="stage5-reconciliation-snapshot",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id}]),
        snapshot_hash=hashlib.sha256(b"stage5-reconciliation").hexdigest(),
        effective_from=datetime(2026, 8, 20, 20, 30),
    )
    decision_run = DecisionRun(
        id="stage5-reconciliation-run",
        strategy_snapshot_id=snapshot.id,
        portfolio_id=portfolio.id,
        run_type="auto_simulation",
        status="SUCCEEDED",
        trade_date=TRADE_DATE,
        decision_at=datetime(2026, 8, 20, 20, 30),
        data_cutoff_at=datetime(2026, 8, 20, 20, 0),
        execution_at=datetime(2026, 8, 21, 9, 30),
        universe_count=1,
        member_count=1,
    )
    evidence = DecisionEvidence(
        id="stage5-reconciliation-evidence",
        decision_run_id=decision_run.id,
        strategy_snapshot_id=snapshot.id,
        portfolio_id=portfolio.id,
        symbol_id=symbol.id,
        trade_date=TRADE_DATE,
        decision_at=decision_run.decision_at,
        data_cutoff_at=decision_run.data_cutoff_at,
        execution_at=decision_run.execution_at,
        action="BUY",
        min_lot_size=100,
        target_quantity=200.0,
        target_qty_delta=200.0,
        intended_price=10.0,
        content_hash=hashlib.sha256(b"stage5-reconciliation-evidence").hexdigest(),
    )
    order = SimOrder(
        portfolio_id=portfolio.id,
        symbol_id=symbol.id,
        side="buy",
        quantity=200.0,
        submitted_price=10.0,
        status="partial",
        filled_quantity=100.0,
        filled_price=10.0,
        filled_amount=1000.0,
        fee=0.3,
        client_order_key="stage5-reconciliation-plan",
        source_type="decision_engine",
        decision_evidence_id=evidence.id,
    )
    # These models intentionally keep their audit foreign keys as scalar
    # fields instead of ORM relationships, so flush their dependencies in
    # order in this isolated SQLite fixture.
    db_session.add(snapshot)
    db_session.flush()
    db_session.add(decision_run)
    db_session.flush()
    db_session.add(evidence)
    db_session.flush()
    db_session.add(order)
    db_session.flush()
    db_session.add_all([
        SimTrade(
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            order_id=order.id,
            side="buy",
            quantity=100.0,
            price=10.0,
            amount=1000.0,
            fee=0.3,
        ),
        Position(
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            quantity=100.0,
            avg_cost=10.0,
            latest_price=10.0,
            market_value=1000.0,
            position_pct=0.01,
            asset_type="stock",
        ),
        CashLedger(
            portfolio_id=portfolio.id,
            entry_type="deposit",
            amount=100_000.0,
            balance_after=100_000.0,
        ),
        CashLedger(
            portfolio_id=portfolio.id,
            entry_type="buy",
            amount=-1_000.3,
            balance_after=98_999.7,
        ),
    ])
    db_session.commit()
    return portfolio


def test_reconciliation_default_path_detects_partial_sim_order(db_session):
    """A partial SimOrder is execution data, not an uncheckable gap."""
    portfolio = _seed_partial_auto_execution(db_session)

    report = reconcile_trade_date(
        db_session,
        portfolio.id,
        TRADE_DATE,
        correlation_id="stage5-reconciliation-bridge",
    )

    assert report.status == "BLOCKED"
    assert any(diff.kind == "UNFILLED_PLAN" for diff in report.differences)
    assert report.expected_cash == 98_999.7
    assert report.actual_cash == 98_999.7
    assert not any(diff.kind == "NAV_BROKEN" for diff in report.differences)
    assert not any(diff.kind == "NOT_CHECKED" for diff in report.differences)
    assert portfolio.last_reconciled_trade_date is None
    assert _get_status(db_session, portfolio) == "RECONCILIATION_BLOCKED"
