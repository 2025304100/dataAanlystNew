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
    from tests.test_portfolio_inspection import _calendar
    _calendar(db_session)
    db_session.flush()
    from app.models.app_setting import AppSetting
    provenance = db_session.get(AppSetting, "market.cn_trade_calendar")
    calendar = json.loads(provenance.value_json)
    calendar["refreshed_at"] = "2026-09-01T00:00:00"
    provenance.value_json = json.dumps(calendar)
    db_session.commit()
    return portfolio, symbol, snapshot



def _account_facts(db, portfolio_id):
    from sqlalchemy import text
    tables = ("sim_orders", "sim_trades", "cash_ledger", "positions", "decision_runs", "decision_evidence", "decision_order_plans")
    return {table: [tuple(row) for row in db.execute(text(
        f"SELECT * FROM {table} WHERE portfolio_id=:pid ORDER BY 1"), {"pid": portfolio_id})] for table in tables}


def _historical_resume(db, portfolio_id, *, through=date(2026, 9, 3)):
    from app.api.routes import portfolios as api
    result = api.portfolio_resume_execute(portfolio_id,
        api.PortfolioResumeExecuteRequest(today=through, dry_run=False), db)
    assert result["created_count"] == result["dispatched_count"] == 0
    assert result["created_tasks"] == []
    assert result["pending_days"]
    assert all(row["execution_review"]["disabled_code"] == "HISTORICAL_EXECUTION_NOT_AUTHORIZED"
               for row in result["pending_days"])
    assert all(row["execution_review"]["next_action"]["target"] == "governance:reconciliation"
               for row in result["pending_days"])
    return result


def test_public_resume_does_not_create_missing_historical_decisions_or_trade_at_backfilled_prices(db_session, monkeypatch):
    portfolio, _, _ = seed_historical_resume(db_session, monkeypatch)
    before = _account_facts(db_session, portfolio.id)
    result = _historical_resume(db_session, portfolio.id)
    assert [row["date"] for row in result["pending_days"]] == ["2026-09-02", "2026-09-03"]
    _historical_resume(db_session, portfolio.id)
    assert _account_facts(db_session, portfolio.id) == before
    assert portfolio.last_decision_trade_date == portfolio.last_reconciled_trade_date == date(2026, 9, 1)


@pytest.mark.parametrize("late_partial", [False, True], ids=["filled", "still_partial"])
def test_public_resume_preserves_later_existing_fill_and_never_fills_an_earlier_missing_decision(db_session, monkeypatch, late_partial):
    from sqlalchemy import select
    from app.models.daily_bar import DailyBar
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch, end_day=5)
    if late_partial:
        db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id,
            DailyBar.trade_date == date(2026, 9, 4))).volume = 300
        db_session.commit()
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    assert first["pending_orders"][0]["remaining_quantity"] == 400
    run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 5))
    before = _account_facts(db_session, portfolio.id)
    _historical_resume(db_session, portfolio.id, through=date(2026, 9, 4))
    assert _account_facts(db_session, portfolio.id) == before
    assert not any(row["trade_date"] == date(2026, 9, 3) for row in _runs(db_session, portfolio.id))


def _runs(db, portfolio_id):
    from sqlalchemy import select
    from app.models.decision_engine import DecisionRun
    return [{"id": row.id, "trade_date": row.trade_date} for row in db.scalars(select(DecisionRun).where(
        DecisionRun.portfolio_id == portfolio_id))]


def test_public_resume_does_not_restart_cancelled_expired_work_or_repeat_original_partial_fills(db_session, monkeypatch):
    from app.models.async_task import AsyncTaskRecord
    from app.services.portfolio_resume_service import resume_idempotency_key
    portfolio, _, snapshot = seed_historical_resume(db_session, monkeypatch)
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    assert first["pending_orders"][0]["remaining_quantity"] == 400
    cancelled = AsyncTaskRecord(id="cancelled-expired-resume", task_type="portfolio_resume", status="cancelled",
        stage="cancelled", cancel_requested=1, is_terminal_locked=1,
        idempotency_key=resume_idempotency_key(portfolio.id, date(2026, 9, 2), "original"),
        payload_json=json.dumps({"portfolio_id": portfolio.id, "trade_date": "2026-09-02",
            "resume_mode": True, "strategy_snapshot_id": snapshot.id}), result_json='{"retained":"original-fill"}')
    db_session.add(cancelled)
    db_session.commit()
    before = _account_facts(db_session, portfolio.id)
    for _attempt in range(2):
        _historical_resume(db_session, portfolio.id)
    db_session.refresh(cancelled)
    assert cancelled.status == "cancelled" and json.loads(cancelled.result_json) == {"retained": "original-fill"}
    assert _account_facts(db_session, portfolio.id) == before


@pytest.mark.parametrize("forced_exit", [False, True], ids=["opposite_signal", "stop_loss"])
def test_public_history_resume_does_not_reinterpret_old_partial_order_with_later_exit_inputs(db_session, monkeypatch, forced_exit):
    from sqlalchemy import select
    from app.models.daily_bar import DailyBar
    from app.models.score import Score
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    assert first["pending_orders"][0]["remaining_quantity"] == 400
    if forced_exit:
        versions = json.loads(snapshot.versions_json or "{}")
        versions["risk_policy"] = {"stop_loss_basis": "average_cost", "stop_loss_pct": .05}
        snapshot.versions_json = json.dumps(versions)
        bar = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id,
            DailyBar.trade_date == date(2026, 9, 3)))
        bar.close, bar.low = 9.5, 9.4
    else:
        score = db_session.scalar(select(Score).where(Score.symbol_id == symbol.id,
            Score.trade_date == date(2026, 9, 3)))
        score.model_alpha_score = score.priority_score = 10
    db_session.commit()
    before = _account_facts(db_session, portfolio.id)
    _historical_resume(db_session, portfolio.id)
    assert _account_facts(db_session, portfolio.id) == before


def test_backfilling_a_historical_score_does_not_authorize_replaying_a_partial_order(db_session, monkeypatch):
    from sqlalchemy import select
    from app.models.score import Score
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch)
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    assert first["pending_orders"][0]["remaining_quantity"] == 400
    score = db_session.scalar(select(Score).where(Score.symbol_id == symbol.id, Score.trade_date == date(2026, 9, 3)))
    db_session.delete(score)
    db_session.commit()
    before = _account_facts(db_session, portfolio.id)
    _historical_resume(db_session, portfolio.id)
    fixed = _make_score(db_session, symbol.id, trade_date=date(2026, 9, 3), priority_score=90)
    fixed.factor_model_run_id, fixed.factor_set_id = snapshot.factor_model_run_id, snapshot.factor_set_id
    db_session.commit()
    _historical_resume(db_session, portfolio.id)
    assert _account_facts(db_session, portfolio.id) == before


def test_historical_diagnostic_does_not_sell_old_positions_and_current_risk_exit_remains_available(db_session, monkeypatch):
    from app.services.sim_accounts import place_sim_order
    portfolio, _, _ = seed_historical_resume(db_session, monkeypatch)
    other = _make_symbol(db_session, symbol="600124")
    order, trade = place_sim_order(db_session, portfolio, other, side="buy", quantity=100, price=10,
        enforce_rules=False, apply_fees=False)
    trade.created_at = order.filled_at = datetime(2026, 9, 1, 2)
    db_session.commit()
    before = _account_facts(db_session, portfolio.id)
    _historical_resume(db_session, portfolio.id)
    assert _account_facts(db_session, portfolio.id) == before
    exit_order, exit_trade = place_sim_order(db_session, portfolio, other, side="sell", quantity=100, price=9.4,
        enforce_rules=False, apply_fees=False)
    assert exit_order.filled_quantity == exit_trade.quantity == 100
    assert exit_trade.side == "sell"


def test_public_resume_keeps_an_expired_partial_sell_immutable(db_session, monkeypatch):
    from sqlalchemy import select
    from app.models.score import Score
    from app.models.sim_account import SimOrder
    from app.services.sim_accounts import place_sim_order
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
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    assert db_session.get(SimOrder, first["executed_orders"][0]["order_id"]).side == "sell"
    assert first["pending_orders"][0]["remaining_quantity"] == 400
    before = _account_facts(db_session, portfolio.id)
    _historical_resume(db_session, portfolio.id)
    assert _account_facts(db_session, portfolio.id) == before


def test_historical_resume_does_not_create_new_entries_even_with_current_cash_and_new_scores(db_session, monkeypatch):
    portfolio, _, snapshot = seed_historical_resume(db_session, monkeypatch)
    first = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    assert first["pending_orders"][0]["remaining_quantity"] == 400
    other = _make_symbol(db_session, symbol="600125")
    _make_member(db_session, portfolio_id=portfolio.id, symbol_id=other.id)
    members = json.loads(snapshot.member_snapshot_json)
    members.append({**members[0], "symbol_id": other.id})
    snapshot.member_snapshot_json = json.dumps(members)
    for day in (2, 3):
        _make_daily_bar(db_session, other.id, trade_date=date(2026, 9, day), close=10)
        score = _make_score(db_session, other.id, trade_date=date(2026, 9, day), priority_score=90)
        score.factor_model_run_id, score.factor_set_id = snapshot.factor_model_run_id, snapshot.factor_set_id
    db_session.commit()
    before = _account_facts(db_session, portfolio.id)
    _historical_resume(db_session, portfolio.id)
    assert _account_facts(db_session, portfolio.id) == before


def test_public_resume_continues_only_the_real_unfinished_frozen_plan_due_today_once(db_session, monkeypatch):
    from sqlalchemy import delete, select
    from app.api.routes import portfolios as api
    from app.models.daily_bar import DailyBar
    from app.models.decision_engine import DecisionOrderPlanRecord, DecisionRun
    from app.models.sim_account import SimOrder, SimTrade
    from app.models.score import Score
    from app.models.scoring_config import ScoringConfig
    from app.services.auto_trade_safety import check_data_health
    from app.services.portfolio_resume_service import review_resume_execution_date
    from app.services.sim_accounts import cash_balance
    from app.services.async_tasks import get_async_task
    from tests.test_execution_lifecycle_public import wait_task
    portfolio, symbol, snapshot = seed_historical_resume(db_session, monkeypatch, end_day=3)
    # Original Sep-2 intent genuinely exists before Sep-3's bar arrives.
    db_session.execute(delete(DailyBar).where(DailyBar.symbol_id == symbol.id,
        DailyBar.trade_date > date(2026, 9, 2)))
    db_session.execute(delete(Score).where(Score.symbol_id == symbol.id,
        Score.trade_date > date(2026, 9, 3)))
    db_session.commit()
    original = run_idempotent_member_source(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 2))
    pending = original["pending_orders"][0]
    assert pending["reason"] == "EXECUTION_BAR_MISSING"
    plan = db_session.get(DecisionOrderPlanRecord, pending["order_plan_id"])
    assert plan.execution_date == date(2026, 9, 3)
    frozen = (plan.order_plan_id, plan.evidence_id, plan.decision_run_id, plan.target_quantity)
    bar = _make_daily_bar(db_session, symbol.id, trade_date=date(2026, 9, 3), close=10.2)
    bar.created_at = datetime(2026, 9, 3, 2)
    # The public execution gate verifies actual publication/model lineage and
    # the current scoring configuration, unlike the low-level engine fixtures.
    db_session.add(ScoringConfig(asset_type="stock", preset_key="frozen-plan-test", name="实际当前配置",
        version=1, is_active=1, config_json=json.dumps({"dimensions": [{"key": "trend", "enabled": True,
            "factors": [{"key": "trend_score", "source": "builtin"}]}]})))
    for score in db_session.scalars(select(Score).where(Score.symbol_id == symbol.id)):
        score.weight_mode = "ridge"
    db_session.commit()
    health = check_data_health(db_session, symbol_id=symbol.id, portfolio_id=portfolio.id)
    assert health.healthy, health.reason
    review = review_resume_execution_date(db_session, portfolio.id, date(2026, 9, 2))
    assert review["enabled"] is True and review["mode"] == "CURRENT_ORIGINAL_PLAN", review
    assert review["source_refs"][0]["decision_run_id"] == original["decision_run_id"]
    result = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 2), dry_run=False), db_session)
    assert result["created_count"] == 1
    task = wait_task(result["created_tasks"][0]["task_id"])
    assert task["status"] == "done", json.dumps(get_async_task(task["id"]).errors, ensure_ascii=False)
    db_session.expire_all()
    plans = list(db_session.scalars(select(DecisionOrderPlanRecord).where(DecisionOrderPlanRecord.portfolio_id == portfolio.id)))
    assert [(row.order_plan_id, row.evidence_id, row.decision_run_id, row.target_quantity) for row in plans] == [frozen]
    orders = list(db_session.scalars(select(SimOrder).where(SimOrder.portfolio_id == portfolio.id)))
    trades = list(db_session.scalars(select(SimTrade).where(SimTrade.portfolio_id == portfolio.id)))
    assert len(orders) == len(trades) == 1
    assert orders[0].decision_evidence_id == plan.evidence_id
    assert trades[0].quantity == orders[0].quantity == orders[0].filled_quantity == plan.target_quantity
    assert trades[0].price == 10.2 and trades[0].created_at.date() == date(2026, 9, 3)
    assert len(list(db_session.scalars(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio.id)))) == 1
    cash = cash_balance(db_session, portfolio.id)
    facts = _account_facts(db_session, portfolio.id)
    again = api.portfolio_resume_execute(portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 2), dry_run=False), db_session)
    assert again["created_count"] == 0
    assert cash_balance(db_session, portfolio.id) == cash
    assert _account_facts(db_session, portfolio.id) == facts
