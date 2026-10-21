"""Complete position reconciliation at the public service seam, isolated SQLite."""
from __future__ import annotations

import hashlib
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.portfolio_reconciliation import reconcile_trade_date


pytestmark = pytest.mark.whitebox
DAY = date(2026, 8, 20)
EXECUTION_DAY = date(2026, 8, 21)


@pytest.fixture
def account():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        p = Portfolio(name="complete-position-reconciliation", account_type="simulated",
            total_capital=100000, investable_ratio=.95, cash_reserve_ratio=.05)
        db.add(p)
        db.add_all([Symbol(id=sid, symbol=f"REC-{sid}", name=f"Stock {sid}",
            asset_type="stock", market="SH") for sid in (1, 2)])
        db.flush()
        snap = StrategyExecutionSnapshot(id="complete-snapshot", snapshot_no=1,
            portfolio_id=p.id, decision_clock_json="{}", member_snapshot_json="[]",
            snapshot_hash=hashlib.sha256(b"complete-snapshot").hexdigest(),
            effective_from=datetime(2026, 8, 20))
        db.add(snap)
        db.flush()
        run = DecisionRun(id="complete-run", strategy_snapshot_id=snap.id,
            portfolio_id=p.id, run_type="auto_simulation", status="SUCCEEDED",
            trade_date=DAY, decision_at=datetime(2026, 8, 20, 20, 30),
            data_cutoff_at=datetime(2026, 8, 20, 20),
            execution_at=datetime(2026, 8, 21, 9, 30), universe_count=1, member_count=1)
        db.add(run)
        db.flush()
        yield db, p, run
    engine.dispose()


def evidence(db, p, run, *, action="HOLD", target=100, delta=0):
    row = DecisionEvidence(id=f"evidence-{run.id}", decision_run_id=run.id,
        strategy_snapshot_id=run.strategy_snapshot_id, portfolio_id=p.id, symbol_id=1,
        trade_date=run.trade_date, decision_at=run.decision_at,
        data_cutoff_at=run.data_cutoff_at, execution_at=run.execution_at,
        action=action, min_lot_size=100, target_quantity=target, target_qty_delta=delta,
        content_hash=hashlib.sha256(run.id.encode()).hexdigest())
    db.add(row)
    db.flush()
    return row


def test_hold_and_unplanned_positions_are_both_checked(account):
    db, p, run = account
    evidence(db, p, run)
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY, trades_provider=lambda *_: [],
        positions_provider=lambda *_: {1: 300, 2: 200})

    assert report.status == "BLOCKED"
    mismatches = {diff.symbol_id: diff for diff in report.differences
        if diff.kind == "POSITION_MISMATCH"}
    assert set(mismatches) == {1, 2}
    assert mismatches[1].expected == {"target_quantity": 100}
    assert mismatches[1].actual == {"final_position_quantity": 300}
    assert mismatches[2].expected == {"target_quantity": 0}
    assert p.last_reconciled_trade_date is None


def test_current_position_is_not_proof_of_a_historical_position(account):
    db, p, run = account
    evidence(db, p, run)
    db.add(Position(portfolio_id=p.id, symbol_id=1, quantity=100,
        avg_cost=10, asset_type="stock"))
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY)

    assert report.status == "BLOCKED"
    assert any(diff.kind == "NOT_CHECKED" and "持仓" in (diff.detail or "")
        for diff in report.differences)
    assert not any(diff.kind == "POSITION_MISMATCH" for diff in report.differences)
    assert p.last_reconciled_trade_date is None


def recorded_fill(db, p, *, sid=1, side="buy", quantity=100, at=None, ev=None,
        balance=99000):
    at = at or datetime(2026, 8, 21, 1, 30)  # UTC: Shanghai 09:30
    amount = quantity * 10
    order = SimOrder(portfolio_id=p.id, symbol_id=sid, side=side, quantity=quantity,
        submitted_price=10, status="filled", filled_quantity=quantity,
        filled_price=10, filled_amount=amount, fee=0, created_at=at, filled_at=at,
        decision_evidence_id=None if ev is None else ev.id)
    db.add(order)
    db.flush()
    db.add(SimTrade(portfolio_id=p.id, symbol_id=sid, order_id=order.id, side=side,
        quantity=quantity, price=10, amount=amount, fee=0, created_at=at))
    db.add(CashLedger(portfolio_id=p.id, entry_type=side,
        amount=-amount if side == "buy" else amount, balance_after=balance,
        ref_type="sim_order", ref_id=order.id, created_at=at))
    db.flush()
    return order


def funding(db, p):
    db.add(CashLedger(portfolio_id=p.id, entry_type="deposit", amount=100000,
        balance_after=100000, ref_type="portfolio", ref_id=p.id,
        note="Initial simulated funding", created_at=datetime(2026, 8, 1)))
    db.flush()


def test_historical_default_path_uses_recorded_fills_not_later_position(account):
    db, p, run = account
    ev = evidence(db, p, run, action="BUY", target=100, delta=100)
    funding(db, p)
    recorded_fill(db, p, ev=ev)
    recorded_fill(db, p, quantity=200, at=datetime(2026, 8, 24, 1, 30), balance=97000)
    db.add(Position(portfolio_id=p.id, symbol_id=1, quantity=300,
        avg_cost=10, asset_type="stock"))
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY)

    assert report.status == "PASSED"
    assert report.actual_position_count == 1
    assert report.differences == []
    assert p.last_reconciled_trade_date == DAY


def test_missing_cash_book_does_not_pass_with_a_matching_dated_position(account):
    db, p, run = account
    evidence(db, p, run)
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY, trades_provider=lambda *_: [],
        positions_provider=lambda *_: {1: 100})

    assert report.status == "BLOCKED"
    assert any(diff.kind == "NOT_CHECKED" and "现金" in (diff.detail or "")
        for diff in report.differences)
    assert p.last_reconciled_trade_date is None


def test_missing_target_quantity_is_incomplete_not_an_empty_target(account):
    db, p, run = account
    evidence(db, p, run, target=None)
    funding(db, p)
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY)

    assert report.status == "BLOCKED"
    assert any(diff.kind == "NOT_CHECKED" and diff.symbol_id == 1
        for diff in report.differences)
    assert p.last_reconciled_trade_date is None


def test_successful_run_without_its_expected_evidence_is_incomplete(account):
    db, p, run = account
    funding(db, p)
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY)

    assert report.status == "BLOCKED"
    assert any(diff.kind == "NOT_CHECKED" and "决策明细" in (diff.detail or "")
        for diff in report.differences)
    assert p.last_reconciled_trade_date is None


@pytest.mark.parametrize("action,target,delta,opening,fill,side", [
    ("HOLD", 100, 0, 100, 0, "buy"),
    ("NO_ACTION", 0, 0, 0, 0, "buy"),
    ("BUY", 200, 100, 100, 100, "buy"),
    ("SELL", 0, -100, 100, 100, "sell"),
])
def test_normal_hold_empty_buy_sell_default_execution_chains_pass(
        account, action, target, delta, opening, fill, side):
    db, p, run = account
    ev = evidence(db, p, run, action=action, target=target, delta=delta)
    funding(db, p)
    balance = 100000
    if opening:
        balance = 99000
        recorded_fill(db, p, at=datetime(2026, 8, 19, 1, 30), balance=balance)
    if fill:
        balance += fill * 10 * (1 if side == "sell" else -1)
        recorded_fill(db, p, side=side, quantity=fill, ev=ev, balance=balance)
    if target:
        db.add(Position(portfolio_id=p.id, symbol_id=1, quantity=target,
            avg_cost=10, asset_type="stock"))
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY)

    assert report.status == "PASSED"
    assert report.differences == []
    assert p.last_reconciled_trade_date == DAY


def test_zero_target_hold_detects_unexpected_existing_position(account):
    db, p, run = account
    evidence(db, p, run, target=0)
    funding(db, p)
    recorded_fill(db, p)
    db.add(Position(portfolio_id=p.id, symbol_id=1, quantity=100,
        avg_cost=10, asset_type="stock"))
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY)

    mismatch = next(diff for diff in report.differences if diff.kind == "POSITION_MISMATCH")
    assert mismatch.expected == {"target_quantity": 0}
    assert mismatch.actual == {"final_position_quantity": 100}
    assert p.last_reconciled_trade_date is None


def test_position_provider_receives_the_actual_execution_day(account):
    db, p, run = account
    evidence(db, p, run, target=0)
    funding(db, p)
    db.commit()

    def dated_positions(_db, _pid, requested_date):
        return {} if requested_date == EXECUTION_DAY else None

    report = reconcile_trade_date(db, p.id, DAY, positions_provider=dated_positions)

    assert report.status == "PASSED"


def test_only_incomplete_evidence_preserves_admin_pause_and_does_not_advance(account):
    from sqlalchemy import text
    from app.services.portfolio_state_machine import _get_status
    db, p, run = account
    evidence(db, p, run)
    db.execute(text("ALTER TABLE portfolios ADD COLUMN portfolio_status VARCHAR(32)"))
    db.execute(text("UPDATE portfolios SET portfolio_status='ADMIN_PAUSED' WHERE id=:pid"),
        {"pid": p.id})
    p.last_reconciled_trade_date = date(2026, 8, 19)
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY)

    assert report.status == "BLOCKED"
    assert report.has_any_diff is True
    assert p.last_reconciled_trade_date == date(2026, 8, 19)
    assert _get_status(db, p) == "ADMIN_PAUSED"


def test_real_replay_seed_can_reconcile_a_later_hold_in_another_portfolio(account):
    from app.models.backtest import BacktestRun, BacktestTrade
    from app.services.backtest_apply import apply_backtest_run_to_portfolio
    db, p, run = account
    evidence(db, p, run)
    source = Portfolio(name="replay-source", account_type="simulated",
        total_capital=100000, investable_ratio=.95, cash_reserve_ratio=.05)
    db.add(source)
    db.flush()
    replay = BacktestRun(portfolio_id=source.id, run_name="verified replay seed",
        symbols_json="[1]", rule_config_json="{}", start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 19), initial_capital=100000, status="success")
    db.add(replay)
    db.flush()
    db.add(BacktestTrade(run_id=replay.id, symbol_id=1, quantity=100,
        entry_date=date(2026, 8, 19), entry_price=10, entry_cost=5))
    db.commit()
    result = apply_backtest_run_to_portfolio(db, replay.id, p.id, clear_existing=True)
    assert result["applied_trades"] == 1

    report = reconcile_trade_date(db, p.id, DAY)

    assert report.status == "PASSED"
    assert report.differences == []
    assert p.last_reconciled_trade_date == DAY


def test_an_empty_connected_execution_source_is_not_a_successful_buy(account):
    db, p, run = account
    evidence(db, p, run, action="BUY", target=100, delta=100)
    funding(db, p)
    db.commit()

    report = reconcile_trade_date(db, p.id, DAY, trades_provider=lambda *_: [],
        positions_provider=lambda *_: {1: 100})

    assert report.status == "BLOCKED"
    assert any(diff.kind == "MISSING_ORDER_PLAN" for diff in report.differences)
    assert p.last_reconciled_trade_date is None
