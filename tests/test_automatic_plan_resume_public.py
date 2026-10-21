"""Real immutable automatic plans survive partial fills and missing next bars."""
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.services.auto_trade_member_source import run_idempotent_member_source
from tests.test_whitebox_auto_trade_idempotent import (
    _apply_member_snapshot, _make_daily_bar, _make_member, _make_portfolio,
    _make_score, _make_symbol,
)

pytestmark = pytest.mark.whitebox


def seed_buy_plan(db, *, execution_bar=True, partial=True, signal_date=date(2026, 7, 17)):
    portfolio = _make_portfolio(db, name="immutable-plan", total_capital=100000)
    portfolio.default_single_position_pct = .02
    symbol = _make_symbol(db, symbol="600123")
    signal = _make_daily_bar(db, symbol.id, trade_date=signal_date, close=10)
    signal.created_at = datetime.combine(signal_date, time(6))
    _make_score(db, symbol.id, trade_date=signal_date, priority_score=90)
    _make_member(db, portfolio_id=portfolio.id, symbol_id=symbol.id)
    snapshot = _apply_member_snapshot(db, portfolio, [{"symbol_id": symbol.id, "execution_mode": "auto"}])
    snapshot.cost_config_json = json.dumps({"commission_rate": .0003, "min_commission": 0,
        "transfer_fee_rate": 0, "stamp_tax_rate": 0, "slippage_buy_bps": 0,
        "slippage_sell_bps": 0, "volume_limit_pct": .5, "min_lot_size": 100})
    if execution_bar:
        bar = _make_daily_bar(db, symbol.id, trade_date=signal_date + timedelta(days=1), close=10)
        bar.volume = 300 if partial else 100000
        bar.created_at = datetime.combine(signal_date + timedelta(days=1), time(2))
    db.commit()
    return portfolio, symbol, snapshot


def test_same_day_partial_retry_keeps_original_decision_and_plan(db_session):
    portfolio, symbol, snapshot = seed_buy_plan(db_session)
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 7, 17))
    assert first["executed_orders"][0]["filled_quantity"] == 100
    assert first["pending_orders"][0]["remaining_quantity"] > 0
    second = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 7, 17))
    assert second["decision_run_id"] == first["decision_run_id"]
    assert second["executed_orders"] == []
    assert second["pending_orders"][0]["remaining_quantity"] == first["pending_orders"][0]["remaining_quantity"]


def test_next_day_restores_waiting_original_plan_before_new_decision(db_session):
    from app.models.score import Score
    portfolio, symbol, snapshot = seed_buy_plan(db_session, execution_bar=False)
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 7, 17))
    pending = first["pending_orders"][0]
    assert pending["reason"] == "EXECUTION_BAR_MISSING"
    original_quantity = pending["quantity"]
    _make_daily_bar(db_session, symbol.id, trade_date=date(2026, 7, 18), close=10)
    score = _make_score(db_session, symbol.id, trade_date=date(2026, 7, 18), priority_score=50, action="hold")
    score.factor_model_run_id = snapshot.factor_model_run_id
    score.factor_set_id = snapshot.factor_set_id
    db_session.commit()
    second = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 7, 18))
    old_fills = [row for row in second["executed_orders"] if row["order_plan_id"] == pending["order_plan_id"]]
    assert len(old_fills) == 1
    assert old_fills[0]["filled_quantity"] == original_quantity
    third = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 7, 18))
    assert third["executed_orders"] == []


def test_next_day_partial_fill_finishes_original_order_once(db_session):
    from app.services.sim_accounts import cash_balance
    portfolio, symbol, snapshot = seed_buy_plan(db_session)
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 7, 17))
    original = first["executed_orders"][0]
    assert original["filled_quantity"] == 100
    assert first["pending_orders"][0]["remaining_quantity"] == 400
    _make_daily_bar(db_session, symbol.id, trade_date=date(2026, 7, 19), close=10)
    score = _make_score(db_session, symbol.id, trade_date=date(2026, 7, 19), priority_score=50, action="hold")
    score.factor_model_run_id, score.factor_set_id = snapshot.factor_model_run_id, snapshot.factor_set_id
    db_session.commit()
    second = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 7, 19))
    filled = [row for row in second["executed_orders"] if row["order_plan_id"] == original["order_plan_id"]]
    assert len(filled) == 1
    assert filled[0]["order_id"] == original["order_id"]
    assert filled[0]["filled_quantity"] == 500
    assert cash_balance(db_session, portfolio.id) == 94998.5
    third = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 7, 19))
    assert third["executed_orders"] == []
    assert cash_balance(db_session, portfolio.id) == 94998.5


def seed_historical_resume(db_session, monkeypatch, *, end_day=4):
    from app.api.routes import portfolios as api
    from app.models.market_data import MarketCalendar
    from app.services import auto_trade_safety, decision_schedule_gate as schedule
    from app.services.sim_accounts import cash_balance
    from tests.test_execution_lifecycle_public import wait_task
    from tests.test_whitebox_auto_trade import _make_active_rule
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, end_day, 12, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(auto_trade_safety, "_now_utc_naive", lambda: datetime(2026, 9, end_day, 12, 30))
    portfolio, symbol, snapshot = seed_buy_plan(db_session, signal_date=date(2026, 9, 2))
    portfolio.last_reconciled_trade_date = portfolio.last_decision_trade_date = date(2026, 9, 1)
    _make_active_rule(db_session, portfolio.id)
    from app.models.daily_bar import DailyBar
    from sqlalchemy import select
    first_bar = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id,
        DailyBar.trade_date == date(2026, 9, 3)))
    first_bar.open = first_bar.close = 10.2
    first_bar.high, first_bar.low = 10.3, 10.1
    later = _make_daily_bar(db_session, symbol.id, trade_date=date(2026, 9, 4), close=10.5)
    later.open, later.high, later.low, later.volume = 10.5, 10.6, 10.4, 2000
    later.created_at = datetime(2026, 9, 4, 2)
    latest = _make_daily_bar(db_session, symbol.id, trade_date=date(2026, 9, 5), close=11)
    latest.volume = 10000
    for day in (3, 4):
        # Keeping a BUY score exercises duplicate-intent suppression while the
        # original order is partially filled, rather than relying on HOLD input.
        score = _make_score(db_session, symbol.id, trade_date=date(2026, 9, day), priority_score=90)
        score.factor_model_run_id, score.factor_set_id = snapshot.factor_model_run_id, snapshot.factor_set_id
    db_session.add_all([MarketCalendar(trade_date=date(2026, 9, d), is_trading_day=1) for d in (2, 3, 4)])
    db_session.commit()
    return portfolio, symbol, snapshot


def test_public_resume_completes_partial_in_daily_order_with_different_prices(db_session, monkeypatch):
    from sqlalchemy import select
    from app.api.routes import portfolios as api
    from app.models.decision_engine import DecisionEvidence, DecisionOrderPlanRecord, DecisionRun
    from app.models.sim_account import SimOrder, SimTrade
    from app.services.sim_accounts import cash_balance
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    result = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False), db_session)
    tasks = [wait_task(item["task_id"]) for item in result["created_tasks"]]
    assert [item["status"] for item in tasks] == ["done", "done", "done"]
    assert tasks[1]["message"] == "当天处理完成，仍有订单待成交"
    db_session.expire_all()
    assert portfolio.last_decision_trade_date == date(2026, 9, 4)
    assert portfolio.last_reconciled_trade_date == date(2026, 9, 1)
    orders = list(db_session.scalars(select(SimOrder).where(SimOrder.portfolio_id == portfolio.id)))
    assert len(orders) == 1 and orders[0].quantity == orders[0].filled_quantity == 500
    trades = list(db_session.scalars(select(SimTrade).where(SimTrade.portfolio_id == portfolio.id).order_by(SimTrade.id)))
    assert [(row.quantity, row.price, row.created_at.date()) for row in trades] == [
        (100, 10.2, date(2026, 9, 3)), (400, 10.5, date(2026, 9, 4))]
    assert cash_balance(db_session, portfolio.id) == pytest.approx(94778.43)
    runs = list(db_session.scalars(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio.id).order_by(DecisionRun.trade_date)))
    assert [row.trade_date for row in runs] == [date(2026, 9, day) for day in (2, 3, 4)]
    mid = db_session.scalar(select(DecisionEvidence).where(DecisionEvidence.decision_run_id == runs[1].id))
    assert mid.action == "HOLD" and mid.target_quantity == 100
    assert "WAITING_EXISTING_ORDER" in json.loads(mid.reason_codes_json)
    plans = list(db_session.scalars(select(DecisionOrderPlanRecord).where(DecisionOrderPlanRecord.decision_run_id.in_([r.id for r in runs]),
        DecisionOrderPlanRecord.action == "BUY")))
    assert len(plans) == 1 and plans[0].target_quantity == 500
    again = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False), db_session)
    assert again["created_count"] == 0
    assert cash_balance(db_session, portfolio.id) == pytest.approx(94778.43)


@pytest.mark.parametrize("late_partial", [False, True], ids=["filled", "still_partial"])
def test_public_resume_refuses_later_existing_fill_before_missing_decision(db_session, monkeypatch, late_partial):
    from sqlalchemy import select
    from app.api.routes import portfolios as api
    from app.models.decision_engine import DecisionRun
    from app.models.daily_bar import DailyBar
    from app.models.sim_account import SimTrade
    from app.services.async_tasks import get_async_task
    from app.services.sim_accounts import cash_balance
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch, end_day=5)
    if late_partial:
        db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id,
            DailyBar.trade_date == date(2026, 9, 4))).volume = 300
        db_session.commit()
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    assert first["pending_orders"][0]["remaining_quantity"] == 400
    # Produce a genuine old fill on Sep 4, without a Sep 3 decision. Its
    # durable retry trace is evidence even for pre-fix wall-clock timestamps.
    run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 5))
    before = [(row.id, row.created_at, row.price, row.quantity) for row in db_session.scalars(
        select(SimTrade).where(SimTrade.portfolio_id == portfolio.id).order_by(SimTrade.id))]
    cash = cash_balance(db_session, portfolio.id)
    result = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False), db_session)
    tasks = [wait_task(item["task_id"]) for item in result["created_tasks"]]
    assert tasks[1]["status"] == "failed"
    failed = get_async_task(result["created_tasks"][1]["task_id"])
    assert "2026-09-04" in str(failed.errors) and "2026-09-03" in str(failed.errors)
    db_session.expire_all()
    assert db_session.scalar(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio.id,
        DecisionRun.trade_date == date(2026, 9, 3))) is None
    assert [(row.id, row.created_at, row.price, row.quantity) for row in db_session.scalars(
        select(SimTrade).where(SimTrade.portfolio_id == portfolio.id).order_by(SimTrade.id))] == before
    assert cash_balance(db_session, portfolio.id) == cash


def test_public_resume_cancel_after_partial_then_retry_keeps_original_fills(db_session, monkeypatch):
    import threading
    from sqlalchemy import event, select
    from app.api.routes import portfolios as api
    from app.models.sim_account import SimOrder, SimTrade
    from app.services.async_tasks import cancel_async_task, get_async_task
    from app.services.sim_accounts import cash_balance
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    request_returned, fired = threading.Event(), []
    task_ids = []
    def cancel_after_real_partial(_conn, _cursor, statement, parameters, _context, _many):
        if statement.startswith("INSERT INTO decision_runs") and "2026-09-03" in parameters and not fired:
            fired.append(True)
            assert request_returned.wait(timeout=3)
            cancel_async_task(task_ids[1])
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", cancel_after_real_partial)
    request = api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False)
    try:
        started = api.portfolio_resume_execute(portfolio.id, request, db_session)
        task_ids.extend(item["task_id"] for item in started["created_tasks"])
        request_returned.set()
        assert [wait_task(tid)["status"] for tid in task_ids] == ["done", "cancelled", "failed"]
    finally:
        request_returned.set()
        event.remove(engine, "before_cursor_execute", cancel_after_real_partial)
    db_session.expire_all()
    original = db_session.scalar(select(SimOrder).where(SimOrder.portfolio_id == portfolio.id))
    assert original.status == "partial" and original.filled_quantity == 100
    assert cash_balance(db_session, portfolio.id) == pytest.approx(98979.69)
    assert portfolio.last_decision_trade_date == date(2026, 9, 2)
    original_id = original.id
    first_trade = db_session.scalar(select(SimTrade).where(SimTrade.order_id == original_id))
    first_record = (first_trade.id, first_trade.price, first_trade.quantity, first_trade.created_at)
    retry = api.portfolio_resume_execute(portfolio.id, request, db_session)
    assert [wait_task(item["task_id"])["status"] for item in retry["created_tasks"]] == ["done", "done"]
    assert get_async_task(task_ids[1]).status == "cancelled"
    db_session.expire_all()
    db_session.refresh(original)
    assert original.id == original_id and original.filled_quantity == 500
    trades = list(db_session.scalars(select(SimTrade).where(SimTrade.order_id == original_id).order_by(SimTrade.id)))
    assert (trades[0].id, trades[0].price, trades[0].quantity, trades[0].created_at) == first_record
    assert len(trades) == 2
    assert cash_balance(db_session, portfolio.id) == pytest.approx(94778.43)
    assert api.portfolio_resume_execute(portfolio.id, request, db_session)["created_count"] == 0
    assert portfolio.last_decision_trade_date == date(2026, 9, 4)


@pytest.mark.parametrize("forced_exit", [False, True], ids=["opposite_signal", "stop_loss"])
def test_public_resume_preserves_exit_conflict_with_unfilled_buy(db_session, monkeypatch, forced_exit):
    from sqlalchemy import select
    from app.api.routes import portfolios as api
    from app.models.daily_bar import DailyBar
    from app.models.decision_engine import DecisionEvidence, DecisionRun
    from app.models.score import Score
    from app.models.sim_account import SimOrder, SimTrade
    from app.services.async_tasks import get_async_task
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    score = db_session.scalar(select(Score).where(Score.symbol_id == symbol.id, Score.trade_date == date(2026, 9, 3)))
    if forced_exit:
        versions = json.loads(snapshot.versions_json or "{}")
        versions["risk_policy"] = {"stop_loss_basis": "average_cost", "stop_loss_pct": .05}
        snapshot.versions_json = json.dumps(versions)
        bar = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date == date(2026, 9, 3)))
        bar.close, bar.low = 9.5, 9.4
    else:
        score.model_alpha_score = score.priority_score = 10
    db_session.commit()
    result = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False), db_session)
    statuses = [wait_task(item["task_id"])["status"] for item in result["created_tasks"]]
    assert statuses == ["done", "failed", "failed"]
    assert "原订单尚未完成" in str(get_async_task(result["created_tasks"][1]["task_id"]).errors)
    db_session.expire_all()
    run = db_session.scalar(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio.id, DecisionRun.trade_date == date(2026, 9, 3)))
    evidence = db_session.scalar(select(DecisionEvidence).where(DecisionEvidence.decision_run_id == run.id))
    assert evidence.action == "REJECTED" and evidence.rejection_reason == "PENDING_ORDER_CONFLICT"
    if forced_exit:
        assert evidence.stop_loss_triggered == 1
    assert portfolio.last_decision_trade_date == date(2026, 9, 2)
    order = db_session.scalar(select(SimOrder).where(SimOrder.portfolio_id == portfolio.id))
    assert order.status == "partial" and order.filled_quantity == 100
    assert len(list(db_session.scalars(select(SimTrade).where(SimTrade.portfolio_id == portfolio.id)))) == 1


def test_public_resume_repeated_data_failure_does_not_repeat_partial_fill(db_session, monkeypatch):
    from sqlalchemy import select
    from app.api.routes import portfolios as api
    from app.models.decision_engine import DecisionRun
    from app.models.score import Score
    from app.models.sim_account import SimTrade
    from app.services.sim_accounts import cash_balance
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    score = db_session.scalar(select(Score).where(Score.symbol_id == symbol.id, Score.trade_date == date(2026, 9, 3)))
    db_session.delete(score)
    db_session.commit()
    request = api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False)
    old_run_ids = []
    for _attempt in range(2):
        result = api.portfolio_resume_execute(portfolio.id, request, db_session)
        statuses = [wait_task(item["task_id"])["status"] for item in result["created_tasks"]]
        assert statuses[-2:] == ["failed", "failed"]
        db_session.expire_all()
        assert len(list(db_session.scalars(select(SimTrade).where(SimTrade.portfolio_id == portfolio.id)))) == 1
        assert cash_balance(db_session, portfolio.id) == pytest.approx(98979.69)
        assert portfolio.last_decision_trade_date == date(2026, 9, 2)
        old_run_ids = list(db_session.scalars(select(DecisionRun.id).where(DecisionRun.portfolio_id == portfolio.id,
            DecisionRun.trade_date == date(2026, 9, 3))))
    assert len(old_run_ids) == 2
    fixed = _make_score(db_session, symbol.id, trade_date=date(2026, 9, 3), priority_score=90)
    fixed.factor_model_run_id, fixed.factor_set_id = snapshot.factor_model_run_id, snapshot.factor_set_id
    db_session.commit()
    result = api.portfolio_resume_execute(portfolio.id, request, db_session)
    assert [wait_task(item["task_id"])["status"] for item in result["created_tasks"]] == ["done", "done"]
    db_session.expire_all()
    assert all(db_session.get(DecisionRun, rid).blocking_status != "READY" for rid in old_run_ids)
    assert len(list(db_session.scalars(select(SimTrade).where(SimTrade.portfolio_id == portfolio.id)))) == 2
    assert cash_balance(db_session, portfolio.id) == pytest.approx(94778.43)


def test_public_resume_pending_buy_does_not_suppress_other_position_stop_loss(db_session, monkeypatch):
    from sqlalchemy import select
    from app.api.routes import portfolios as api
    from app.models.decision_engine import DecisionEvidence, DecisionRun
    from app.models.sim_account import SimTrade
    from app.services.sim_accounts import place_sim_order
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    other = _make_symbol(db_session, symbol="600124")
    _make_member(db_session, portfolio_id=portfolio.id, symbol_id=other.id)
    members = json.loads(snapshot.member_snapshot_json)
    members.append({**members[0], "symbol_id": other.id})
    snapshot.member_snapshot_json = json.dumps(members)
    versions = json.loads(snapshot.versions_json or "{}")
    versions["risk_policy"] = {"stop_loss_basis": "average_cost", "stop_loss_pct": .05}
    snapshot.versions_json = json.dumps(versions)
    order, trade = place_sim_order(db_session, portfolio, other, side="buy", quantity=100, price=10,
        enforce_rules=False, apply_fees=False)
    trade.created_at = order.filled_at = datetime(2026, 9, 1, 2)
    for day, price in ((2, 10), (3, 9.4), (4, 9.5)):
        bar = _make_daily_bar(db_session, other.id, trade_date=date(2026, 9, day), close=price)
        bar.created_at = datetime(2026, 9, day, 2)
        score = _make_score(db_session, other.id, trade_date=date(2026, 9, day), priority_score=50, action="hold")
        score.factor_model_run_id, score.factor_set_id = snapshot.factor_model_run_id, snapshot.factor_set_id
    db_session.commit()
    result = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False), db_session)
    assert [wait_task(item["task_id"])["status"] for item in result["created_tasks"]] == ["done", "done", "done"]
    db_session.expire_all()
    run = db_session.scalar(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio.id, DecisionRun.trade_date == date(2026, 9, 3)))
    exit_evidence = db_session.scalar(select(DecisionEvidence).where(DecisionEvidence.decision_run_id == run.id,
        DecisionEvidence.symbol_id == other.id))
    assert exit_evidence.action == "SELL" and exit_evidence.stop_loss_triggered == 1
    trades = list(db_session.scalars(select(SimTrade).where(SimTrade.portfolio_id == portfolio.id,
        SimTrade.symbol_id == other.id, SimTrade.side == "sell")))
    assert len(trades) == 1 and trades[0].quantity == 100 and trades[0].created_at.date() == date(2026, 9, 4)


def test_public_resume_same_stop_loss_keeps_original_partial_sell(db_session, monkeypatch):
    from sqlalchemy import select
    from app.api.routes import portfolios as api
    from app.models.decision_engine import DecisionEvidence, DecisionRun
    from app.models.score import Score
    from app.models.sim_account import SimOrder, SimTrade
    from app.services.sim_accounts import place_sim_order
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    for score in db_session.scalars(select(Score).where(Score.symbol_id == symbol.id)):
        score.model_alpha_score = score.priority_score = 10
        score.action = "exit"
    versions = json.loads(snapshot.versions_json or "{}")
    versions["risk_policy"] = {"stop_loss_basis": "average_cost", "stop_loss_pct": .05}
    snapshot.versions_json = json.dumps(versions)
    order, trade = place_sim_order(db_session, portfolio, symbol, side="buy", quantity=500, price=11,
        enforce_rules=False, apply_fees=False)
    trade.created_at = order.filled_at = datetime(2026, 9, 1, 2)
    db_session.commit()
    result = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False), db_session)
    assert [wait_task(item["task_id"])["status"] for item in result["created_tasks"]] == ["done", "done", "done"]
    db_session.expire_all()
    run = db_session.scalar(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio.id, DecisionRun.trade_date == date(2026, 9, 3)))
    evidence = db_session.scalar(select(DecisionEvidence).where(DecisionEvidence.decision_run_id == run.id))
    assert evidence.action == "HOLD" and evidence.stop_loss_triggered == 1 and evidence.target_quantity == 400
    sells = list(db_session.scalars(select(SimOrder).where(SimOrder.portfolio_id == portfolio.id, SimOrder.side == "sell")))
    assert len(sells) == 1 and sells[0].quantity == sells[0].filled_quantity == 500
    trades = list(db_session.scalars(select(SimTrade).where(SimTrade.order_id == sells[0].id).order_by(SimTrade.id)))
    assert [(row.quantity, row.price) for row in trades] == [(100, 10.2), (400, 10.5)]


def test_public_resume_pending_buy_pauses_new_entries_at_total_exposure_limit(db_session, monkeypatch):
    from sqlalchemy import select
    from app.api.routes import portfolios as api
    from app.models.decision_engine import DecisionEvidence, DecisionRun
    from app.models.sim_account import SimOrder
    from app.services.sim_accounts import cash_balance
    from app.services.async_tasks import get_async_task
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    versions = json.loads(snapshot.versions_json or "{}")
    # Filled inventory alone is below 6%, while the old BUY's remainder plus
    # another stock's 5% BUY would exceed it despite plenty of available cash.
    versions["rule_constraints"] = {"total_investable_pct": .06, "min_cash_reserve_pct": .000001,
        "max_loss_per_trade_pct": 0, "asset_limits": {"stock": .06, "etf": 0}, "sector_limit_pct": 1,
        "single_limit_pct": .05, "stage_limits": {"stock": {"default_open_pct": .05}},
        "open_slot_max": 20, "rule_ok": True}
    snapshot.versions_json = json.dumps(versions)
    other = _make_symbol(db_session, symbol="600125")
    _make_member(db_session, portfolio_id=portfolio.id, symbol_id=other.id)
    members = json.loads(snapshot.member_snapshot_json)
    members.append({**members[0], "symbol_id": other.id})
    snapshot.member_snapshot_json = json.dumps(members)
    for day in (2, 3, 4):
        bar = _make_daily_bar(db_session, other.id, trade_date=date(2026, 9, day), close=10)
        bar.created_at = datetime(2026, 9, day, 2)
        score = _make_score(db_session, other.id, trade_date=date(2026, 9, day), priority_score=50 if day == 2 else 90)
        score.factor_model_run_id, score.factor_set_id = snapshot.factor_model_run_id, snapshot.factor_set_id
    db_session.commit()
    result = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 4), dry_run=False), db_session)
    assert [wait_task(item["task_id"])["status"] for item in result["created_tasks"]] == ["done", "done", "done"]
    db_session.expire_all()
    run = db_session.scalar(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio.id, DecisionRun.trade_date == date(2026, 9, 3)))
    evidence = db_session.scalar(select(DecisionEvidence).where(DecisionEvidence.decision_run_id == run.id, DecisionEvidence.symbol_id == other.id))
    assert evidence.action != "BUY" and evidence.target_quantity == 0
    assert "PENDING_BUY_ENTRY_PAUSED" in json.loads(evidence.reason_codes_json)
    assert evidence.blocking_reason == "原买单尚未完成，暂缓新增买入"
    pending = get_async_task(result["created_tasks"][1]["task_id"]).result["execution_result"]
    assert pending["pending_order_count"] == 1 and pending["pending_orders"][0]["remaining_quantity"] == 400
    assert pending["pending_order_plan_ids"] == [pending["pending_orders"][0]["order_plan_id"]]
    assert db_session.scalar(select(SimOrder).where(SimOrder.portfolio_id == portfolio.id, SimOrder.symbol_id == other.id)) is None
    assert cash_balance(db_session, portfolio.id) == pytest.approx(94778.43)


def test_live_start_keeps_original_partial_state_until_next_actual_session(db_session, monkeypatch):
    from sqlalchemy import select
    from app.api.routes import portfolio_governance as api
    from app.models.decision_engine import DecisionRun
    from app.models.sim_account import SimTrade
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch, end_day=3)
    original = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    assert original["pending_orders"][0]["remaining_quantity"] == 400
    started = api.start_auto_simulation(portfolio.id,
        api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 3)), db_session, "owner")
    assert wait_task(started["task_id"])["status"] == "partial"
    db_session.expire_all()
    assert len(list(db_session.scalars(select(SimTrade).where(SimTrade.portfolio_id == portfolio.id)))) == 1
    assert portfolio.last_decision_trade_date == date(2026, 9, 1)
    assert db_session.scalar(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio.id,
        DecisionRun.trade_date == date(2026, 9, 3))) is None
