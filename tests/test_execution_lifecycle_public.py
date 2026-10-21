"""Single-machine execution lifecycle through public service/API boundaries."""
from datetime import date, datetime, timezone

import pytest

from app.services.decision_schedule_gate import ensure_auto_simulation_schedule

pytestmark = pytest.mark.whitebox


def test_evening_utc_request_is_checked_as_shanghai_time():
    result = ensure_auto_simulation_schedule(
        datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc), portfolio_id=1,
    )
    assert result.allowed is True
    assert result.effective_data_as_of_trade_date == date(2026, 9, 2)
    assert result.effective_data_cutoff_at.isoformat() == "2026-09-02T20:00:00"


@pytest.fixture
def ready_portfolio(db_session):
    from app.models.portfolio import Portfolio
    from app.services.portfolio_state_machine import transition_portfolio_state
    p = Portfolio(name="lifecycle-test", account_type="simulated", asset_scope="mixed",
                  total_capital=100000, investable_ratio=1, cash_reserve_ratio=0,
                  default_single_position_pct=.2, auto_trade_enabled=1,
                  last_decision_trade_date=date(2026, 9, 1),
                  last_reconciled_trade_date=date(2026, 9, 1))
    db_session.add(p)
    db_session.flush()
    transition_portfolio_state(db_session, p.id, "READY")
    db_session.commit()
    return p


def test_preflight_is_read_only_and_uses_server_clock(db_session, ready_portfolio, monkeypatch):
    from app.api.routes import portfolio_governance as api
    from app.services import decision_schedule_gate as schedule
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(db_session, "commit", lambda: pytest.fail("checking committed state"))
    result = api.preflight_auto_simulation(ready_portfolio.id,
        api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2),
            decision_at=datetime(2026, 9, 2, 4, tzinfo=timezone.utc)), db_session, "owner")
    assert result.proceed is True
    assert result.from_state == result.to_state == "READY"
    assert api.get_portfolio_status(ready_portfolio.id, db_session).current_state == "READY"


def wait_task(task_id):
    import time
    from app.services.async_tasks import get_async_task_heartbeat, is_worker_running
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = get_async_task_heartbeat(task_id)
        if value and value["status"] not in {"queued", "running"} and not is_worker_running(task_id):
            return value
        time.sleep(.025)
    pytest.fail(f"task did not finish: {task_id}")


def test_real_start_finishes_as_failed_when_strategy_is_missing(db_session, ready_portfolio, monkeypatch):
    from app.api.routes import portfolio_governance as api
    from app.services import decision_schedule_gate as schedule
    from app.services.async_tasks import get_async_task
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    started = api.start_auto_simulation(ready_portfolio.id,
        api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2)), db_session, "owner")
    result = wait_task(started["task_id"])
    assert result["status"] == "failed"
    task = get_async_task(started["task_id"])
    assert task.errors and "策略" in task.errors[0]["detail_zh"]
    db_session.expire_all()
    assert api.get_portfolio_status(ready_portfolio.id, db_session).current_state == "READY"
    assert ready_portfolio.last_decision_trade_date == date(2026, 9, 1)


def test_start_rejects_a_future_client_clock_and_creates_no_task(db_session, ready_portfolio, monkeypatch):
    from fastapi import HTTPException
    from app.api.routes import portfolio_governance as api
    from app.services import decision_schedule_gate as schedule
    from app.services.async_tasks import list_async_tasks
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc))
    with pytest.raises(HTTPException) as error:
        api.start_auto_simulation(ready_portfolio.id,
            api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2),
                decision_at=datetime(2026, 9, 2, 21)), db_session, "owner")
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "SCHEDULE_TOO_EARLY"
    assert list_async_tasks(task_type="portfolio_auto_simulation") == []
    assert api.get_portfolio_status(ready_portfolio.id, db_session).current_state == "READY"


def test_resume_reattaches_old_queued_task_and_stops_after_real_failure(db_session, ready_portfolio, monkeypatch):
    import json
    from app.api.routes import portfolios as api
    from app.models.async_task import AsyncTaskRecord
    from app.models.market_data import MarketCalendar
    from app.services import decision_schedule_gate as schedule
    from app.services.portfolio_resume_service import resume_idempotency_key
    from app.services.async_tasks import get_async_task
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc))
    # Old versions created this queued record without starting any worker.
    old_id = "a" * 32
    db_session.add_all([
        MarketCalendar(trade_date=date(2026, 9, 2), is_trading_day=1),
        MarketCalendar(trade_date=date(2026, 9, 3), is_trading_day=1),
        AsyncTaskRecord(id=old_id, task_type="portfolio_resume", status="queued", stage="queued",
            idempotency_key=resume_idempotency_key(ready_portfolio.id, date(2026, 9, 2), "old"),
            payload_json=json.dumps({"resume_mode": True, "portfolio_id": ready_portfolio.id,
                "trade_date": "2026-09-02"})),
    ])
    db_session.commit()
    result = api.portfolio_resume_execute(ready_portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 3), dry_run=False), db_session)
    assert old_id in [task["task_id"] for task in result["created_tasks"]]
    assert wait_task(old_id)["status"] == "failed"
    for task in result["created_tasks"]:
        completed = wait_task(task["task_id"])
        assert completed["status"] == "failed"
    assert "策略" in get_async_task(old_id).errors[0]["detail_zh"]
    db_session.expire_all()
    assert ready_portfolio.last_decision_trade_date == date(2026, 9, 1)


def test_evaluate_cannot_use_client_evening_to_bypass_server_daytime(db_session, ready_portfolio, monkeypatch):
    from fastapi import HTTPException
    from app.api.routes import decision_engine as api
    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.schemas.decision_engine import DecisionEvaluateRequest
    from app.services import decision_schedule_gate as schedule
    db_session.add(StrategyExecutionSnapshot(id="clock-test", portfolio_id=ready_portfolio.id,
        snapshot_no=1, snapshot_hash="clock-test", snapshot_type="save_and_apply",
        decision_clock_json="{}", member_snapshot_json="[]", effective_from=datetime(2026, 9, 1)))
    db_session.commit()
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc))
    with pytest.raises(HTTPException) as error:
        api.evaluate_portfolio(ready_portfolio.id, DecisionEvaluateRequest(strategy_snapshot_id="clock-test",
            trade_date=date(2026, 9, 2), decision_at=datetime(2026, 9, 2, 21), run_type="auto_simulation",
            persist=True), db_session, "owner")
    assert error.value.status_code == 400
    assert error.value.detail["error"] == "SCHEDULE_TOO_EARLY"


def test_resume_cannot_execute_future_days_from_client_date(db_session, ready_portfolio, monkeypatch):
    from fastapi import HTTPException
    from app.api.routes import portfolios as api
    from app.services import decision_schedule_gate as schedule
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc))
    with pytest.raises(HTTPException) as error:
        api.portfolio_resume_execute(ready_portfolio.id,
            api.PortfolioResumeExecuteRequest(today=date(2026, 9, 3), dry_run=False), db_session)
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "eval.portfolio.resume_future_date"


def seed_approved_no_action_portfolio(db, portfolio, *, score_value=50.0):
    import json
    from app.models.daily_bar import DailyBar
    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.models.portfolio import Position
    from app.models.symbol import Symbol
    from app.api.routes import portfolio_governance as api
    from app.services.sim_accounts import ensure_sim_account_seed
    from tests.portfolio_binding_helpers import bind_applied_snapshot
    from tests.test_whitebox_auto_trade import _make_active_rule
    symbol = Symbol(symbol="600001", name="保留原持仓", asset_type="stock", market="SH")
    db.add(symbol)
    db.flush()
    db.add_all([Position(portfolio_id=portfolio.id, symbol_id=symbol.id, quantity=100, avg_cost=10,
        asset_type="stock", latest_price=10, market_value=1000, position_pct=.01),
        DailyBar(symbol_id=symbol.id, trade_date=date(2026, 9, 2), open=10, high=10, low=10, close=10,
            volume=100000, created_at=datetime(2026, 9, 2, 6))])
    snapshot = StrategyExecutionSnapshot(id="approved-lifecycle", portfolio_id=portfolio.id,
        snapshot_no=1, snapshot_hash="approved-lifecycle", snapshot_type="save_and_apply",
        decision_clock_json=json.dumps({"decision_at": "20:30 Asia/Shanghai"}),
        member_snapshot_json="[]", effective_from=datetime(2026, 9, 1, 1))
    if score_value >= 70:
        from app.models.portfolio_member import PortfolioMember
        snapshot.member_snapshot_json = json.dumps([{"symbol_id": symbol.id, "execution_mode": "auto"}])
        db.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbol.id, execution_mode="auto", status="active"))
    db.add(snapshot)
    bind_applied_snapshot(db, snapshot)
    from tests.test_g1_decision_engine import _insert_scores
    _insert_scores(db, snapshot.factor_model_run_id, [
        (symbol.id, date(2026, 9, d), score_value, datetime(2026, 9, d, 6)) for d in (2, 3)])
    _make_active_rule(db, portfolio.id)
    ensure_sim_account_seed(db, portfolio)
    portfolio.status_model = "MODEL_INACTIVE"
    db.commit()
    api.confirm_portfolio_model_binding(portfolio.id, api.ConfirmModelBindingRequest(
        snapshot_id=snapshot.id, ack=True, review_note="已核对本次模型策略和范围，同意使用这份策略"), db, "owner")
    portfolio.last_decision_trade_date = date(2026, 9, 1)
    db.commit()


def test_start_reaches_real_engine_and_publishes_completed_task(db_session, ready_portfolio, monkeypatch):
    from app.api.routes import portfolio_governance as api
    from app.api.routes.decision_engine import get_decision_run
    from app.services import decision_schedule_gate as schedule
    from app.services.async_tasks import get_async_task
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    seed_approved_no_action_portfolio(db_session, ready_portfolio)
    started = api.start_auto_simulation(ready_portfolio.id,
        api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2)), db_session, "owner")
    finished = wait_task(started["task_id"])
    assert finished["status"] == "done", [e["detail_zh"] for e in get_async_task(started["task_id"]).errors]
    task = get_async_task(started["task_id"])
    run_id = task.result["execution_result"]["decision_run_id"]
    assert get_decision_run(run_id, db_session).id == run_id
    from app.services.decision_schedule_gate import build_today_preview_view
    assert build_today_preview_view(db_session, ready_portfolio.id, date(2026, 9, 2)).summary["status"] == "SUCCEEDED"
    assert task.result["execution_result"]["executed_orders"] == []
    db_session.expire_all()
    assert ready_portfolio.last_decision_trade_date == date(2026, 9, 2)
    assert api.get_portfolio_status(ready_portfolio.id, db_session).current_state == "READY"


@pytest.mark.parametrize("attributes", [
    {"status": "BLOCKED", "diffs_count_non_checked": 0},
    {"status": "PASSED", "diffs_count_non_checked": 0, "incomplete_checks_count": 1},
])
def test_unverified_reconciliation_is_visible_as_an_alert(db_session, ready_portfolio, attributes):
    from app.api.routes import portfolio_governance as api
    from app.services.data_governance_audit import write_audit_event
    write_audit_event(db_session, "RECONCILIATION_RESULT", portfolio_id=ready_portfolio.id,
        attributes=attributes, operator_id="owner")
    db_session.commit()
    page = api.get_portfolio_audit_events(ready_portfolio.id, action=None, operator_id=None,
        occurred_from=None, occurred_to=None, attributes_q=None, page=1, page_size=20, db=db_session, actor="owner")
    assert page.items[0].severity == "L2"


def test_resume_runs_real_days_then_repeating_it_creates_nothing(db_session, ready_portfolio, monkeypatch):
    from app.api.routes import portfolios as api
    from app.models.market_data import MarketCalendar
    from app.services import decision_schedule_gate as schedule
    from app.services.async_tasks import get_async_task
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc))
    seed_approved_no_action_portfolio(db_session, ready_portfolio)
    db_session.add_all([MarketCalendar(trade_date=date(2026, 9, d), is_trading_day=1) for d in (2, 3)])
    db_session.commit()
    request = api.PortfolioResumeExecuteRequest(today=date(2026, 9, 3), dry_run=False)
    result = api.portfolio_resume_execute(ready_portfolio.id, request, db_session)
    assert len(result["created_tasks"]) == 2
    for task in result["created_tasks"]:
        assert wait_task(task["task_id"])["status"] == "done", [e["detail_zh"] for e in get_async_task(task["task_id"]).errors]
    db_session.expire_all()
    assert ready_portfolio.last_decision_trade_date == date(2026, 9, 3)
    repeated = api.portfolio_resume_execute(ready_portfolio.id, request, db_session)
    assert repeated["created_tasks"] == []
    assert repeated["created_count"] == 0


def test_resume_database_failure_can_be_retried_without_duplicate_decisions(db_session, ready_portfolio, monkeypatch):
    from sqlalchemy import event
    from app.api.routes import portfolios as api
    from app.api.routes.decision_engine import list_portfolio_decision_runs
    from app.models.market_data import MarketCalendar
    from app.services import decision_schedule_gate as schedule
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    seed_approved_no_action_portfolio(db_session, ready_portfolio)
    db_session.add(MarketCalendar(trade_date=date(2026, 9, 2), is_trading_day=1))
    db_session.commit()
    fired = []
    def fail_first_decision(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO decision_runs") and not fired:
            fired.append(True)
            raise RuntimeError("isolated database failure")
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", fail_first_decision)
    request = api.PortfolioResumeExecuteRequest(today=date(2026, 9, 2), dry_run=False)
    try:
        first = api.portfolio_resume_execute(ready_portfolio.id, request, db_session)
        assert wait_task(first["created_tasks"][0]["task_id"])["status"] == "failed"
    finally:
        event.remove(engine, "before_cursor_execute", fail_first_decision)
    db_session.expire_all()
    assert ready_portfolio.last_decision_trade_date == date(2026, 9, 1)
    second = api.portfolio_resume_execute(ready_portfolio.id, request, db_session)
    assert wait_task(second["created_tasks"][0]["task_id"])["status"] == "done"
    assert first["created_tasks"][0]["task_id"] != second["created_tasks"][0]["task_id"]
    runs = list_portfolio_decision_runs(ready_portfolio.id, limit=20, offset=0, db=db_session)
    assert runs["total"] == 1


def test_cancelled_real_worker_stops_without_advancing_decision_date(db_session, ready_portfolio, monkeypatch):
    import threading
    from sqlalchemy import event
    from app.api.routes import portfolio_governance as api
    from app.services import decision_schedule_gate as schedule
    from app.services.async_tasks import cancel_async_task, get_async_task
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    seed_approved_no_action_portfolio(db_session, ready_portfolio)
    request_returned = threading.Event()
    task_ids = []
    fired = []
    def request_cancel_at_database_boundary(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO decision_runs") and not fired:
            fired.append(True)
            assert request_returned.wait(timeout=3)
            cancel_async_task(task_ids[0])
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", request_cancel_at_database_boundary)
    try:
        started = api.start_auto_simulation(ready_portfolio.id,
            api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2)), db_session, "owner")
        task_ids.append(started["task_id"])
        request_returned.set()
        assert wait_task(started["task_id"])["status"] == "cancelled", get_async_task(started["task_id"]).errors
    finally:
        request_returned.set()
        event.remove(engine, "before_cursor_execute", request_cancel_at_database_boundary)
    db_session.expire_all()
    assert ready_portfolio.last_decision_trade_date == date(2026, 9, 1)
    assert api.get_portfolio_status(ready_portfolio.id, db_session).current_state == "READY"
    request = api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2))
    assert api.preflight_auto_simulation(ready_portfolio.id, request, db_session, "owner").proceed is True
    retry = api.start_auto_simulation(ready_portfolio.id, request, db_session, "owner")
    assert wait_task(retry["task_id"])["status"] == "done"
    assert retry["task_id"] != started["task_id"]


def test_partial_task_can_pass_preflight_and_resume_original_plan(db_session, ready_portfolio, monkeypatch):
    from app.api.routes import portfolio_governance as api
    from app.services import decision_schedule_gate as schedule
    from app.services.async_tasks import get_async_task
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    from app.services import auto_trade_safety
    monkeypatch.setattr(auto_trade_safety, "_now_utc_naive", lambda: datetime(2026, 9, 2, 12, 30))
    seed_approved_no_action_portfolio(db_session, ready_portfolio, score_value=90)
    request = api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2))
    first = api.start_auto_simulation(ready_portfolio.id, request, db_session, "owner")
    assert wait_task(first["task_id"])["status"] == "partial"
    first_result = get_async_task(first["task_id"]).result["execution_result"]
    db_session.expire_all()
    assert api.preflight_auto_simulation(ready_portfolio.id, request, db_session, "owner").proceed is True
    retry = api.start_auto_simulation(ready_portfolio.id, request, db_session, "owner")
    assert wait_task(retry["task_id"])["status"] == "partial"
    retried_result = get_async_task(retry["task_id"]).result["execution_result"]
    assert retried_result["decision_run_id"] == first_result["decision_run_id"]
    assert retried_result["pending_orders"][0]["order_plan_id"] == first_result["pending_orders"][0]["order_plan_id"]
    assert retried_result["executed_orders"] == []


def test_failed_start_retries_when_task_business_key_is_unique(db_session, ready_portfolio, monkeypatch):
    from sqlalchemy import text
    from app.api.routes import portfolio_governance as api
    from app.services import decision_schedule_gate as schedule
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    db_session.execute(text("CREATE UNIQUE INDEX qa_execution_unique_key ON async_tasks(idempotency_key)"))
    db_session.commit()
    request = api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2))
    first = api.start_auto_simulation(ready_portfolio.id, request, db_session, "owner")
    assert wait_task(first["task_id"])["status"] == "failed"
    db_session.expire_all()
    second = api.start_auto_simulation(ready_portfolio.id, request, db_session, "owner")
    assert wait_task(second["task_id"])["status"] == "failed"
    assert first["task_id"] != second["task_id"]


def test_second_resume_does_not_start_a_later_day_while_first_is_running(db_session, ready_portfolio, monkeypatch):
    import threading
    from sqlalchemy import event
    from app.api.routes import portfolios as api
    from app.models.market_data import MarketCalendar
    from app.services import decision_schedule_gate as schedule
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc))
    seed_approved_no_action_portfolio(db_session, ready_portfolio)
    db_session.add_all([MarketCalendar(trade_date=date(2026, 9, d), is_trading_day=1) for d in (2, 3)])
    db_session.commit()
    entered, release = threading.Event(), threading.Event()
    def pause_first_decision(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO decision_runs") and not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", pause_first_decision)
    tasks = []
    try:
        first = api.portfolio_resume_execute(ready_portfolio.id,
            api.PortfolioResumeExecuteRequest(today=date(2026, 9, 3), max_days=1, dry_run=False), db_session)
        tasks.extend(first["created_tasks"])
        assert entered.wait(timeout=3)
        second = api.portfolio_resume_execute(ready_portfolio.id,
            api.PortfolioResumeExecuteRequest(today=date(2026, 9, 3), max_days=2, dry_run=False), db_session)
        tasks.extend(second["created_tasks"])
        assert second["created_tasks"] == []
    finally:
        release.set()
        for task in tasks:
            wait_task(task["task_id"])
        event.remove(engine, "before_cursor_execute", pause_first_decision)


def test_missing_scores_can_be_repaired_and_retried_with_original_strategy(db_session, ready_portfolio, monkeypatch):
    from sqlalchemy import select
    from app.api.routes import portfolio_governance as api
    from app.api.routes.decision_engine import list_portfolio_decision_runs
    from app.models.score import Score
    from app.services import decision_schedule_gate as schedule
    from app.services.async_tasks import get_async_task
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    seed_approved_no_action_portfolio(db_session, ready_portfolio)
    score = db_session.scalar(select(Score).where(Score.trade_date == date(2026, 9, 2)))
    score.published_at = datetime(2026, 9, 2, 15)  # Not available at the saved cutoff.
    db_session.commit()
    request = api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2))
    first = api.start_auto_simulation(ready_portfolio.id, request, db_session, "owner")
    assert wait_task(first["task_id"])["status"] == "failed"
    original = list_portfolio_decision_runs(ready_portfolio.id, limit=20, offset=0, db=db_session)
    assert original["total"] == 1
    original_id = original["items"][0].id
    assert original["items"][0].blocking_status != "READY"
    score.published_at = datetime(2026, 9, 2, 6)
    db_session.commit()
    assert api.preflight_auto_simulation(ready_portfolio.id, request, db_session, "owner").proceed is True
    second = api.start_auto_simulation(ready_portfolio.id, request, db_session, "owner")
    assert wait_task(second["task_id"])["status"] == "done", get_async_task(second["task_id"]).errors
    final = list_portfolio_decision_runs(ready_portfolio.id, limit=20, offset=0, db=db_session)
    assert final["total"] == 2
    assert {item.strategy_snapshot_id for item in final["items"]} == {"approved-lifecycle"}
    assert next(item for item in final["items"] if item.id == original_id).blocking_status != "READY"
    db_session.expire_all()
    assert api.preflight_auto_simulation(ready_portfolio.id, request, db_session, "owner").proceed is False


def test_resume_requires_a_real_trading_calendar_before_execution(db_session, ready_portfolio, monkeypatch):
    from app.api.routes import portfolios as api
    from app.services import decision_schedule_gate as schedule
    from app.services.async_tasks import get_async_task
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc))
    seed_approved_no_action_portfolio(db_session, ready_portfolio)
    # The planning fallback guesses weekdays when the calendar is empty.
    # Such a date must never authorize a real calculation or trade.
    result = api.portfolio_resume_execute(ready_portfolio.id,
        api.PortfolioResumeExecuteRequest(today=date(2026, 9, 2), dry_run=False), db_session)
    task_id = result["created_tasks"][0]["task_id"]
    assert wait_task(task_id)["status"] == "failed"
    assert "交易日历" in get_async_task(task_id).errors[0]["detail_zh"]
    db_session.expire_all()
    assert ready_portfolio.last_decision_trade_date == date(2026, 9, 1)
