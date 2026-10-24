"""Persistent automatic checks must not become implicit account recovery."""
from datetime import date, datetime, timedelta, timezone
import json
import hashlib
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.async_task import AsyncTaskRecord
from app.models.app_setting import AppSetting
from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio
from app.models.portfolio_reconciliation_coverage import (
    PortfolioAccountBaseline, PortfolioReconciliationCursor, PortfolioReconciliationReport,
    PortfolioReconciliationDay,
)
from app.models.scheduled_task import ScheduledTask
from app.models.sim_account import CashLedger
from app.models.symbol import Symbol
from app.services import async_tasks
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services import portfolio_reconciliation_job as job
from app.services.portfolio_reconciliation import ReconciliationReport


pytestmark = pytest.mark.whitebox
DAY = date(2026, 10, 20)
AT = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "reconciliation-jobs.db"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE portfolios ADD COLUMN portfolio_status VARCHAR(32)"))
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(job, "get_session_local", lambda: factory)
    monkeypatch.setattr(async_tasks, "_pick_session_factory", lambda *_: factory)
    monkeypatch.setattr(async_tasks, "is_worker_stop_requested", lambda *_: False)
    monkeypatch.setattr(job, "_now", lambda: AT)
    started = []
    monkeypatch.setattr(async_tasks, "_start_worker", lambda tid, worker: started.append((tid, worker)))
    with factory() as db:
        p = Portfolio(name="job-account", account_type="simulated", total_capital=100000,
                      investable_ratio=.95, cash_reserve_ratio=.05, effective_start_date=DAY)
        db.add(p)
        db.flush()
        pid = p.id
        db.add(CashLedger(portfolio_id=pid, entry_type="deposit", amount=100000,
            balance_after=100000, ref_type="portfolio", ref_id=pid,
            note="Initial simulated funding", created_at=datetime(2026, 10, 19)))
        # Isolated authoritative-adapter fixture: complete year, explicit
        # closures and provenance. This is never a production calendar seed.
        year_start = date(2026, 1, 1)
        weekdays = [year_start + timedelta(days=n) for n in range(365)
                    if (year_start + timedelta(days=n)).weekday() < 5]
        closed = set(weekdays[:21])
        open_count = 0
        for n in range(365):
            day = year_start + timedelta(days=n)
            is_open = int(day.weekday() < 5 and day not in closed)
            open_count += is_open
            db.add(MarketCalendar(trade_date=day, is_trading_day=is_open))
        db.add(AppSetting(key="market.cn_trade_calendar", updated_by="exchange-calendar-sync", value_json=json.dumps({
            "source": "https://www.sse.com.cn/disclosure/dealinstruc/closed/", "year": 2026,
            "refreshed_at": "2026-10-21T00:00:00+00:00", "trading_days": open_count})))
        db.execute(text("UPDATE portfolios SET portfolio_status='READY' WHERE id=:pid"), {"pid": pid})
        db.commit()
    yield SimpleNamespace(engine=engine, factory=factory, pid=pid, started=started)
    engine.dispose()


@pytest.fixture
def ready(store, monkeypatch):
    coverage = {"cursor_id": "cursor-one", "generation": 1, "baseline_id": "baseline-one",
        "coverage_start_trade_date": DAY, "verified_through_trade_date": None,
        "row_version": 1, "calendar_version": "calendar-accepted", "baseline_snapshot": {}}
    calendar = {"status": "PASSED", "source": "verified-exchange-calendar", "version": "calendar-current",
                "expected_trade_date": DAY + timedelta(days=2), "reason": "真实交易日历"}
    monkeypatch.setattr(job, "_coverage", lambda db, pid, **kw: (dict(coverage), None))
    monkeypatch.setattr(job, "resolve_inspection_calendar", lambda *a, **kw: dict(calendar))
    monkeypatch.setattr(job, "get_reconciliation_queue", lambda db, pid, **kw: {
        "target_trade_date": DAY.isoformat(), "calendar_status": "KNOWN", "days": []})
    calls = []
    version = {"value": "a" * 64}
    def reconcile(db, pid, day, *, persist, **kw):
        calls.append((pid, day, persist))
        return ReconciliationReport(portfolio_id=pid, trade_date=day, decision_run_id="run-one", status="PASSED",
            verdict="PASSED", work_state="done", report_id=f"report::{pid}::{day}", report_version=version["value"],
            actions=[{"code": "VIEW_RECONCILIATION_REPORT", "enabled": True}],
            progression={"can_advance": True}, audit=SimpleNamespace(event_id=17) if persist else None)
    monkeypatch.setattr(job, "reconcile_trade_date", reconcile)
    return SimpleNamespace(store=store, coverage=coverage, calendar=calendar, calls=calls, version=version)


def _task(store, tid):
    with store.factory() as db:
        row = db.get(AsyncTaskRecord, tid)
        return row, json.loads(row.payload_json), json.loads(row.result_json or "{}")


def test_actual_uncovered_account_is_unknown_without_legacy_report_or_cursor_write(store):
    with store.factory() as db:
        p = db.get(Portfolio, store.pid)
        p.last_reconciled_trade_date = DAY
        db.commit()
    task = job.create_portfolio_reconciliation_task(portfolio_id=store.pid)
    job.run_portfolio_reconciliation_worker(task.id)
    row, _, result = _task(store, task.id)
    assert row.status == "done"
    item = result["items"][0]
    assert item["verdict"] == "HISTORICAL_COVERAGE_UNKNOWN"
    assert item["target_trade_date"] is None
    assert item["can_reconcile"] is False
    with store.factory() as db:
        assert db.get(Portfolio, store.pid).last_reconciled_trade_date == DAY
        assert not list(db.scalars(select(DataGovernanceAuditEvent)))
        assert len(list(db.scalars(select(CashLedger)))) == 1


def test_broken_baseline_reference_stays_unknown_with_an_account_review_action(store):
    with store.factory() as db:
        db.add(PortfolioReconciliationCursor(id="missing-baseline-cursor", portfolio_id=store.pid,
            baseline_id="baseline-does-not-exist", coverage_start_trade_date=DAY, generation=1,
            calendar_version="calendar-real", policy_version="test", row_version=1))
        db.commit()
        plan = job.build_portfolio_reconciliation_job_plan(db, portfolio_id=store.pid)
    item = plan["items"][0]
    assert item["verdict"] == "BASELINE_SOURCE_MISSING"
    assert item["target_trade_date"] is None
    assert item["actions"][0]["code"] == "REVIEW_COVERAGE"
    assert item["can_reconcile"] is False


def test_calendar_unknown_never_invents_a_target_or_flushes_pending_changes(ready, monkeypatch):
    monkeypatch.setattr(job, "resolve_inspection_calendar", lambda *a, **kw: {
        "status": "CALENDAR_UNKNOWN", "expected_trade_date": None, "reason": "来源未知"})
    with ready.store.factory() as db:
        p = db.get(Portfolio, ready.store.pid)
        p.name = "not-flushed"
        flushed = []
        event.listen(db, "before_flush", lambda *a: flushed.append(True))
        plan = job.build_portfolio_reconciliation_job_plan(db, portfolio_id=p.id)
        assert plan["items"][0]["verdict"] == "CALENDAR_UNKNOWN"
        assert plan["items"][0]["target_trade_date"] is None
        assert not flushed
    assert not ready.calls


def test_later_requested_date_cannot_bypass_earliest_unverified_day(ready):
    with ready.store.factory() as db:
        plan = job.build_portfolio_reconciliation_job_plan(db, portfolio_id=ready.store.pid,
            trade_date=DAY + timedelta(days=1))
    item = plan["items"][0]
    assert item["verdict"] == "PREVIOUS_DATE_UNVERIFIED"
    assert item["target_trade_date"] == DAY.isoformat()
    assert item["can_reconcile"] is False
    assert not ready.calls


def test_future_target_waits_and_worker_does_not_persist_or_advance(ready, monkeypatch):
    ready.coverage["coverage_start_trade_date"] = DAY + timedelta(days=3)
    monkeypatch.setattr(job, "get_reconciliation_queue", lambda *a, **kw: {
        "target_trade_date": (DAY + timedelta(days=3)).isoformat(), "calendar_status": "KNOWN"})
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    job.run_portfolio_reconciliation_worker(task.id)
    _, _, result = _task(ready.store, task.id)
    assert result["items"][0]["verdict"] == "NOT_DUE"
    assert result["items"][0]["work_state"] == "waiting_execution"
    assert not any(persist for _, _, persist in ready.calls)


def test_today_after_close_without_a_run_waits_for_t_plus_one_and_does_not_pause(ready, monkeypatch):
    from app.services.portfolio_reconciliation import reconcile_trade_date
    today = AT.astimezone(job.ZoneInfo("Asia/Shanghai")).date()
    ready.coverage["coverage_start_trade_date"] = today
    assert ready.calendar["expected_trade_date"] == today  # Today is already bar-due at 16:00.
    monkeypatch.setattr(job, "get_reconciliation_queue", lambda *a, **kw: {
        "target_trade_date": today.isoformat(), "calendar_status": "KNOWN"})
    monkeypatch.setattr(job, "reconcile_trade_date", reconcile_trade_date)
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    job.run_portfolio_reconciliation_worker(task.id)
    _, _, result = _task(ready.store, task.id)
    assert result["items"][0]["verdict"] == "NOT_DUE"
    assert result["items"][0]["work_state"] == "waiting_execution"
    with ready.store.factory() as db:
        p = db.get(Portfolio, ready.store.pid)
        assert p.status_reconciliation == "READY"
        assert p.last_reconciled_trade_date is None
        assert db.scalar(text("SELECT portfolio_status FROM portfolios WHERE id=:pid"), {"pid": p.id}) == "READY"
        assert not list(db.scalars(select(DataGovernanceAuditEvent)))


def test_replayed_source_starts_only_creator_and_report_audit_does_not_change_identity(ready):
    first = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=9)
    with ready.store.factory() as db:
        write_audit_event(db, "RECONCILIATION_RESULT", portfolio_id=ready.store.pid,
            attributes={"trade_date": DAY.isoformat(), "status": "PASSED", "report_version": "a" * 64})
        db.commit()
    ready.coverage["row_version"] += 1
    second = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    assert first.id == second.id
    assert [tid for tid, _ in ready.store.started] == [first.id]
    with ready.store.factory() as db:
        assert len(list(db.scalars(select(AsyncTaskRecord)))) == 1


@pytest.mark.parametrize("change", ["sources", "calendar", "generation"])
def test_evidence_calendar_or_coverage_generation_produces_new_identity(ready, change):
    first = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    if change == "sources":
        ready.version["value"] = "b" * 64
    elif change == "calendar":
        ready.calendar["version"] = "calendar-new"
    else:
        ready.coverage["generation"] += 1
    second = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    assert first.id != second.id
    assert len(ready.store.started) == 2


@pytest.mark.parametrize("terminal", ["failed", "partial"])
def test_failed_retry_has_new_attempt_but_repeated_retry_reuses_it(ready, terminal):
    first = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    with ready.store.factory() as db:
        row = db.get(AsyncTaskRecord, first.id)
        row.status, row.stage, row.is_terminal_locked = terminal, terminal, 1
        db.commit()
    retry = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    replay = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    assert retry.id != first.id
    assert replay.id == retry.id
    assert _task(ready.store, retry.id)[1]["attempt"] == 2
    assert _task(ready.store, first.id)[0].status == terminal
    assert len(ready.store.started) == 2


def test_cancelled_task_and_result_are_not_overwritten_or_implicitly_restarted(ready):
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    with ready.store.factory() as db:
        row = db.get(AsyncTaskRecord, task.id)
        row.status, row.stage, row.is_terminal_locked, row.cancel_requested = "cancelled", "cancelled", 1, 1
        row.result_json = '{"cancelled_evidence":"keep-me"}'
        db.commit()
    job.run_portfolio_reconciliation_worker(task.id)
    assert job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid).id == task.id
    assert not job._publish(task.id, status="done", result_json='{"wrong":true}')
    row, _, result = _task(ready.store, task.id)
    assert row.status == "cancelled"
    assert result == {"cancelled_evidence": "keep-me"}
    assert len(ready.store.started) == 1


def test_single_portfolio_failure_does_not_hide_other_portfolio_report(ready, monkeypatch):
    with ready.store.factory() as db:
        other = Portfolio(name="second-account", account_type="simulated", total_capital=100000,
            investable_ratio=.95, cash_reserve_ratio=.05)
        db.add(other)
        db.commit()
        other_id = other.id
    original = job.reconcile_trade_date
    def reconcile(db, pid, day, **kw):
        if pid == ready.store.pid and kw["persist"]:
            raise RuntimeError("one-source-unavailable")
        return original(db, pid, day, **kw)
    monkeypatch.setattr(job, "reconcile_trade_date", reconcile)
    task = job.create_portfolio_reconciliation_task()
    job.run_portfolio_reconciliation_worker(task.id)
    row, _, result = _task(ready.store, task.id)
    assert row.status == "partial"
    assert [(item["portfolio_id"], item["verdict"]) for item in result["items"]] == [
        (ready.store.pid, "READ_ERROR"), (other_id, "PASSED")]
    assert result["items"][1]["report_version"] == "a" * 64


def test_same_day_same_generation_report_version_is_reused(ready, monkeypatch):
    monkeypatch.setattr(job, "latest_reconciliation_results", lambda db, pid: {DAY: {
        "coverage_generation": 1, "baseline_id": "baseline-one", "report_version": "a" * 64,
        "persistent_report_id": "immutable-report-one", "persistent_report_version": 3, "audit_event_id": 29}})
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    job.run_portfolio_reconciliation_worker(task.id)
    _, _, result = _task(ready.store, task.id)
    assert result["items"][0]["reused_report"] is True
    assert result["items"][0]["persistent_report_id"] == "immutable-report-one"
    assert result["items"][0]["audit_event_id"] == 29
    assert not any(persist for _, _, persist in ready.calls)


def test_old_generation_report_is_never_reused(ready, monkeypatch):
    monkeypatch.setattr(job, "latest_reconciliation_results", lambda db, pid: {DAY: {
        "coverage_generation": 0, "baseline_id": "baseline-old", "report_version": "a" * 64}})
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    job.run_portfolio_reconciliation_worker(task.id)
    assert any(persist for _, _, persist in ready.calls)


def _durable_work_scope(ready):
    """Real work rows with an independently stubbed account-evidence reader."""
    with ready.store.factory() as db:
        acceptance = write_audit_event(db, "DATA_BLOCK_RESOLUTION", portfolio_id=ready.store.pid,
            attributes={"confirmation_type": "ACCOUNT_BASELINE_ACCEPTED", "snapshot_id": "baseline-one", "ack": True})
        db.add(PortfolioAccountBaseline(id="baseline-one", portfolio_id=ready.store.pid, scope_key="account", kind="initial",
            as_of_at=datetime(2026, 10, 19, 12), coverage_start_trade_date=DAY, snapshot_json="{}", content_hash="a" * 64,
            source_refs_json="[]", history_gaps_json="[]", accepted_audit_id=acceptance.event_id,
            acceptance_key="durable-work-test", request_hash="b" * 64))
        db.flush()
        db.add(PortfolioReconciliationCursor(id="cursor-one", portfolio_id=ready.store.pid, scope_key="account",
            baseline_id="baseline-one", generation=1, coverage_start_trade_date=DAY, calendar_version="calendar-accepted",
            policy_version="durable-work-test", row_version=1))
        db.commit()


def test_transient_read_failures_persist_one_five_fifteen_minute_retries_then_need_review(ready, monkeypatch):
    _durable_work_scope(ready)
    clock = {"at": AT}
    monkeypatch.setattr(job, "_now", lambda: clock["at"])
    def unavailable(*a, **kw):
        raise RuntimeError("private source unavailable")
    monkeypatch.setattr(job, "reconcile_trade_date", unavailable)
    for attempt, delay in enumerate((1, 5, 15, None), start=1):
        task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
        job.run_portfolio_reconciliation_worker(task.id)
        with ready.store.factory() as db:
            day = db.scalar(select(PortfolioReconciliationDay))
            assert day.attempt_count == attempt
            assert day.requested_evidence_version > day.processed_evidence_version
            assert day.last_error_code == "RECONCILIATION_TRANSIENT_READ_ERROR"
            if delay is None:
                assert day.work_state == "needs_review" and day.next_check_at is None
            else:
                assert day.work_state == "waiting_data"
                assert day.next_check_at == (clock["at"] + timedelta(minutes=delay)).replace(tzinfo=None)
        if delay is not None:
            waiting = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
            job.run_portfolio_reconciliation_worker(waiting.id)
            with ready.store.factory() as db:
                assert db.scalar(select(PortfolioReconciliationDay)).attempt_count == attempt
            clock["at"] += timedelta(minutes=delay)
    with ready.store.factory() as db:
        view = job.get_portfolio_reconciliation_jobs(db, ready.store.pid)
        assert view["work_items"][0]["retry_exhausted"] is True
        assert view["work_items"][0]["attempt_count"] == 4
        assert not list(db.scalars(select(PortfolioReconciliationReport)))


def test_event_during_check_keeps_a_new_requested_version_and_requires_a_new_task(ready, monkeypatch):
    _durable_work_scope(ready)
    original = job.reconcile_trade_date
    def incoming_event(db, pid, target, *, persist, **kwargs):
        if persist:
            request = job.request_reconciliation_check(db, pid)
            assert request["requested_day_ids"]
        return original(db, pid, target, persist=persist, **kwargs)
    monkeypatch.setattr(job, "reconcile_trade_date", incoming_event)
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
    job.run_portfolio_reconciliation_worker(task.id)
    with ready.store.factory() as db:
        day = db.scalar(select(PortfolioReconciliationDay))
        assert day.requested_evidence_version > day.processed_evidence_version
        assert day.work_state == "queued"
    following = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
    assert following.id != task.id


def test_dispatch_failure_and_backend_restart_keep_persistent_work_for_later_scan(ready, monkeypatch):
    _durable_work_scope(ready)
    def refused(*a):
        raise RuntimeError("worker start failed")
    monkeypatch.setattr(async_tasks, "_start_worker", refused)
    with pytest.raises(RuntimeError):
        job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
    with ready.store.factory() as db:
        day = db.scalar(select(PortfolioReconciliationDay))
        assert day.last_error_code == "RECONCILIATION_DISPATCH_FAILED"
        assert day.work_state == "waiting_data" and day.requested_evidence_version > day.processed_evidence_version
        assert day.next_check_at == (AT + timedelta(minutes=1)).replace(tzinfo=None)
    monkeypatch.setattr(job, "_now", lambda: AT + timedelta(minutes=1))
    monkeypatch.setattr(async_tasks, "_start_worker", lambda *a: None)
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
    with ready.store.factory() as db:
        day = db.scalar(select(PortfolioReconciliationDay))
        day.last_error_code, day.next_check_at, day.work_state = None, None, "running"
        row = db.get(AsyncTaskRecord, task.id)
        row.status, row.stage, row.is_terminal_locked = "failed", "interrupted", 1
        db.commit()
    job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
    with ready.store.factory() as db:
        day = db.scalar(select(PortfolioReconciliationDay))
        assert day.last_error_code == "BACKEND_RESTART_INTERRUPTED"
        assert day.next_check_at == (AT + timedelta(minutes=2)).replace(tzinfo=None)
        assert day.requested_evidence_version > day.processed_evidence_version


def test_waiting_execution_has_a_persistent_next_check_without_consuming_error_budget(ready):
    _durable_work_scope(ready)
    future = AT.date()
    with ready.store.factory() as db:
        cursor = db.get(PortfolioReconciliationCursor, "cursor-one")
        cursor.coverage_start_trade_date = future
        db.commit()
    ready.coverage["coverage_start_trade_date"] = future
    original_queue = job.get_reconciliation_queue
    job.get_reconciliation_queue = lambda *a, **kw: {"target_trade_date": future.isoformat(), "calendar_status": "KNOWN"}
    try:
        task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
        job.run_portfolio_reconciliation_worker(task.id)
        with ready.store.factory() as db:
            day = db.scalar(select(PortfolioReconciliationDay))
            assert day.work_state == "waiting_execution"
            assert day.next_check_at and day.attempt_count == 0
            assert day.requested_evidence_version > day.processed_evidence_version
    finally:
        job.get_reconciliation_queue = original_queue


def test_structured_source_read_error_uses_retry_budget_instead_of_becoming_a_completed_business_check(ready, monkeypatch):
    _durable_work_scope(ready)
    def unavailable(db, pid, target, **kwargs):
        return ReconciliationReport(portfolio_id=pid, trade_date=target, decision_run_id=None, status="BLOCKED",
            verdict="EVIDENCE_INCOMPLETE", work_state="needs_review", reason_codes=["READ_ERROR"],
            report_id="source-read-error", report_version="c" * 64,
            activity_evidence=[{"source_code": "sim_orders", "source_label": "模拟订单", "query_status": "READ_ERROR", "count": None}])
    monkeypatch.setattr(job, "reconcile_trade_date", unavailable)
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid, schedule_id=101)
    job.run_portfolio_reconciliation_worker(task.id)
    with ready.store.factory() as db:
        day = db.scalar(select(PortfolioReconciliationDay))
        assert day.work_state == "waiting_data"
        assert day.attempt_count == 1
        assert day.next_check_at == (AT + timedelta(minutes=1)).replace(tzinfo=None)
        assert not list(db.scalars(select(PortfolioReconciliationReport)))


def test_worker_rechecks_coverage_calendar_and_report_with_current_reads(ready, monkeypatch):
    observed = {"coverage": [], "calendar": [], "report": []}
    coverage, calendar, reconcile = job._coverage, job.resolve_inspection_calendar, job.reconcile_trade_date
    def read_coverage(*args, **kwargs):
        observed["coverage"].append(kwargs.get("current_read", False))
        return coverage(*args, **kwargs)
    def read_calendar(*args, **kwargs):
        observed["calendar"].append(kwargs.get("current_read", False))
        return calendar(*args, **kwargs)
    def read_report(*args, **kwargs):
        observed["report"].append((kwargs["persist"], kwargs.get("current_read", False)))
        return reconcile(*args, **kwargs)
    monkeypatch.setattr(job, "_coverage", read_coverage)
    monkeypatch.setattr(job, "resolve_inspection_calendar", read_calendar)
    monkeypatch.setattr(job, "reconcile_trade_date", read_report)
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    assert observed == {"coverage": [False], "calendar": [False], "report": [(False, False)]}
    job.run_portfolio_reconciliation_worker(task.id)
    assert observed["coverage"][-1] is True
    assert observed["calendar"][-1] is True
    assert observed["report"][-2:] == [(False, True), (True, True)]
    assert _task(ready.store, task.id)[0].status == "done"


def test_two_task_workers_serialize_checks_for_same_portfolio(ready, monkeypatch):
    first = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    ready.version["value"] = "b" * 64
    second = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    original = job.reconcile_trade_date
    guard, active, max_active = threading.Lock(), 0, 0
    def reconcile(*args, **kwargs):
        nonlocal active, max_active
        if kwargs["persist"]:
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(.08)
                return original(*args, **kwargs)
            finally:
                with guard:
                    active -= 1
        return original(*args, **kwargs)
    monkeypatch.setattr(job, "reconcile_trade_date", reconcile)
    workers = [threading.Thread(target=job.run_portfolio_reconciliation_worker, args=(task.id,))
               for task in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert not any(worker.is_alive() for worker in workers)
    assert max_active == 1
    assert [_task(ready.store, task.id)[0].status for task in (first, second)] == ["done", "done"]


def test_cancel_during_work_keeps_the_cancellation_result(ready, monkeypatch):
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    def cancel_during_item(tid, item, cid):
        with ready.store.factory() as db:
            row = db.get(AsyncTaskRecord, tid)
            row.status, row.stage, row.is_terminal_locked, row.cancel_requested = "cancelled", "cancelled", 1, 1
            row.result_json = '{"cancelled_evidence":"before-worker-return"}'
            db.commit()
        return {"portfolio_id": item["portfolio_id"], "verdict": "PASSED"}
    monkeypatch.setattr(job, "_run_item", cancel_during_item)
    job.run_portfolio_reconciliation_worker(task.id)
    row, _, result = _task(ready.store, task.id)
    assert row.status == "cancelled"
    assert result == {"cancelled_evidence": "before-worker-return"}


def test_real_covered_funded_account_persists_a_report_and_contiguous_cursor_without_trading(store, monkeypatch):
    from app.services.portfolio_reconciliation_coverage import _canonical, _hash
    from app.models.sim_account import SimOrder, SimTrade
    with store.factory() as db:
        funding = db.scalar(select(CashLedger).where(CashLedger.portfolio_id == store.pid))
        baseline = {"as_of_at": "2026-10-19T12:00:00", "cash": "100000", "positions": [], "generation": 1,
            "source_watermarks": [{"source": "cash_ledger", "row_ids": [str(funding.id)]},
                {"source": "sim_orders", "row_ids": []}, {"source": "sim_trades", "row_ids": []}]}
        acceptance = write_audit_event(db, "DATA_BLOCK_RESOLUTION", portfolio_id=store.pid,
            attributes={"confirmation_type": "ACCOUNT_BASELINE_ACCEPTED", "snapshot_id": "real-baseline", "ack": True})
        db.add(PortfolioAccountBaseline(id="real-baseline", portfolio_id=store.pid, scope_key="account", kind="initial",
            as_of_at=datetime(2026, 10, 19, 12), coverage_start_trade_date=DAY,
            snapshot_json=_canonical(baseline), content_hash=_hash(baseline), source_refs_json="[]", history_gaps_json="[]",
            accepted_audit_id=acceptance.event_id, acceptance_key="real-job-test", request_hash="e" * 64))
        db.flush()
        db.add(PortfolioReconciliationCursor(id="real-cursor", portfolio_id=store.pid, scope_key="account", generation=1,
            baseline_id="real-baseline", coverage_start_trade_date=DAY,
            calendar_version=job.resolve_inspection_calendar(db, at=AT)["version"],
            policy_version="test-real-account", row_version=1))
        db.add(Symbol(id=101, symbol="REAL-JOB", name="Real job test", asset_type="stock", market="SH"))
        db.add(StrategyExecutionSnapshot(id="real-job-snapshot", portfolio_id=store.pid, snapshot_no=1,
            decision_clock_json="{}", member_snapshot_json="[]", snapshot_hash=hashlib.sha256(b"real-job").hexdigest(),
            effective_from=datetime(2026, 10, 19)))
        db.flush()
        db.add(DecisionRun(id="real-job-run", strategy_snapshot_id="real-job-snapshot", portfolio_id=store.pid,
            run_type="auto_simulation", status="SUCCEEDED", trade_date=DAY,
            decision_at=datetime(2026, 10, 20, 12), data_cutoff_at=datetime(2026, 10, 20, 11),
            execution_at=datetime(2026, 10, 21, 1, 30), universe_count=1, member_count=1))
        db.flush()
        db.add(DecisionEvidence(id="real-job-evidence", decision_run_id="real-job-run", strategy_snapshot_id="real-job-snapshot",
            portfolio_id=store.pid, symbol_id=101, trade_date=DAY, decision_at=datetime(2026, 10, 20, 12),
            data_cutoff_at=datetime(2026, 10, 20, 11), execution_at=datetime(2026, 10, 21, 1, 30),
            action="HOLD", target_quantity=0, target_qty_delta=0, min_lot_size=100,
            content_hash=hashlib.sha256(b"real-job-evidence").hexdigest()))
        db.get(Portfolio, store.pid).last_decision_trade_date = DAY
        db.commit()
    task = job.create_portfolio_reconciliation_task(portfolio_id=store.pid, trade_date=DAY)
    job.run_portfolio_reconciliation_worker(task.id)
    row, _, result = _task(store, task.id)
    assert row.status == "done"
    item = result["items"][0]
    assert item["verdict"] == "PASSED", item
    assert item["persistent_report_id"]
    with store.factory() as db:
        assert db.get(PortfolioReconciliationCursor, "real-cursor").verified_through_trade_date == DAY
        assert len(list(db.scalars(select(PortfolioReconciliationReport)))) == 1
        assert not list(db.scalars(select(SimOrder)))
        assert not list(db.scalars(select(SimTrade)))
        assert len(list(db.scalars(select(CashLedger)))) == 1
    assert job.create_portfolio_reconciliation_task(portfolio_id=store.pid, trade_date=DAY).id == task.id


def test_history_filters_scope_before_limit_and_reads_without_patrol_or_autoflush(ready, monkeypatch):
    task = job.create_portfolio_reconciliation_task(portfolio_id=ready.store.pid)
    with ready.store.factory() as db:
        db.add(ScheduledTask(name="all-account-check", task_type=job.TASK_TYPE, enabled=1, payload_json="{}"))
        db.add(ScheduledTask(name="other-account-check", task_type=job.TASK_TYPE, enabled=1,
                             payload_json='{"portfolio_id":98765}'))
        for n in range(30):
            db.add(AsyncTaskRecord(id=f"unrelated-{n}", task_type=job.TASK_TYPE, status="queued", stage="queued",
                payload_json='{"portfolio_id":98765}', created_at=AT.replace(tzinfo=None) + timedelta(minutes=n + 1)))
        db.commit()
    monkeypatch.setattr(async_tasks, "get_async_task", lambda *a, **kw: pytest.fail("GET must not patrol"))
    with ready.store.factory() as db:
        p = db.get(Portfolio, ready.store.pid)
        p.name = "unflushed-name"
        flushes = []
        event.listen(db, "before_flush", lambda *a: flushes.append(True))
        history = job.get_portfolio_reconciliation_jobs(db, p.id, limit=1)
        assert [item["id"] for item in history["jobs"]] == [task.id]
        assert [item["name"] for item in history["schedules"]] == ["all-account-check"]
        assert not flushes
    assert _task(ready.store, task.id)[0].status == "queued"


def test_shared_job_response_has_no_other_or_unknown_scope_envelopes(store):
    pid = store.pid
    with store.factory() as db:
        db.add(ScheduledTask(name="shared-check", task_type=job.TASK_TYPE, enabled=1, payload_json="{}",
                             last_error="PRIVATE_SCHEDULE_ERROR"))
        db.add(AsyncTaskRecord(id="shared-private-envelope", task_type=job.TASK_TYPE, status="partial",
            stage="PRIVATE_UNSCOPED_STAGE", message="PRIVATE_UNSCOPED_MESSAGE", total=99, processed=99,
            current_item="PRIVATE_CURRENT_ITEM", suggested_action="PRIVATE_SUGGESTED_ACTION",
            correlation_id="PRIVATE_CORRELATION", idempotency_key="PRIVATE_IDEMPOTENCY",
            payload_json=json.dumps({"portfolio_ids": [pid, 98765], "attempt": 2, "private": "PRIVATE_PAYLOAD"}),
            batch_recovery_json=json.dumps({"portfolio_id": 98765, "secret": "PRIVATE_CHECKPOINT"}),
            errors_json=json.dumps([
                {"portfolio_id": 98765, "code": "other", "detail_zh": "PRIVATE_OTHER_ERROR"},
                {"code": "unknown", "detail_zh": "PRIVATE_NO_PORTFOLIO_ERROR"},
                {"portfolio_id": pid, "code": "mine", "detail_zh": "current-scope-error",
                 "evidence": {"secret": "PRIVATE_NESTED_ERROR"}},
            ]),
            result_json=json.dumps({"portfolio_count": 99, "unknown": "PRIVATE_UNKNOWN_RESULT", "items": [
                {"portfolio_id": pid, "verdict": "PASSED", "reason": "current-scope-reason",
                 "report_id": "my-report", "report_version": "a" * 64, "private": "PRIVATE_UNKNOWN_ITEM",
                 "actions": [
                    {"portfolio_id": pid, "code": "VIEW_REPORT", "label": "查看当前组合报告", "secret": "PRIVATE_ACTION_FIELD"},
                    {"portfolio_id": 98765, "code": "OTHER", "label": "PRIVATE_OTHER_ACTION"},
                    {"code": "UNKNOWN", "label": "PRIVATE_UNSCOPED_ACTION"}],
                 "progression": {"can_advance": True, "unknown": "PRIVATE_PROGRESSION"}},
                {"portfolio_id": 98765, "verdict": "READ_ERROR", "reason": "PRIVATE_UNRELATED_RESULT"},
                {"verdict": "READ_ERROR", "reason": "PRIVATE_UNSCOPED_RESULT"},
            ]})))
        db.commit()
        history = job.get_portfolio_reconciliation_jobs(db, pid)
    assert "PRIVATE_" not in json.dumps(history, ensure_ascii=False)
    view = history["jobs"][0]
    assert view["result"]["items"] == view["items"]
    assert [item["portfolio_id"] for item in view["result"]["items"]] == [pid]
    assert view["total"] == view["processed"] == view["ok_count"] == 1
    assert view["result"]["portfolio_count"] == 1
    assert view["result"]["error_count"] == 0
    assert view["errors"] == [{"portfolio_id": pid, "code": "mine", "detail_zh": "current-scope-error"}]
    assert view["message"] == "current-scope-reason"
    assert view["items"][0]["report_id"] == "my-report"
    assert view["items"][0]["actions"][0]["code"] == "VIEW_REPORT"
    assert {"payload_json", "batch_recovery", "batch_recovery_json", "current_item", "suggested_action"}.isdisjoint(view)


def test_account_event_only_advances_matching_enabled_check_schedules_and_coalesces(store):
    later = AT.replace(tzinfo=None) + timedelta(hours=2)
    earlier = AT.replace(tzinfo=None) - timedelta(minutes=1)
    with store.factory() as db:
        schedules = [
            ScheduledTask(name="global", task_type=job.TASK_TYPE, enabled=1, payload_json="{}", next_run_at=later),
            ScheduledTask(name="mine", task_type=job.TASK_TYPE, enabled=1,
                payload_json=json.dumps({"portfolio_id": store.pid}), next_run_at=later),
            ScheduledTask(name="already", task_type=job.TASK_TYPE, enabled=1, payload_json="{}", next_run_at=earlier),
            ScheduledTask(name="disabled", task_type=job.TASK_TYPE, enabled=0, payload_json="{}", next_run_at=later),
            ScheduledTask(name="other", task_type=job.TASK_TYPE, enabled=1,
                payload_json='{"portfolio_id":98765}', next_run_at=later),
            ScheduledTask(name="trading", task_type="portfolio_auto_run", enabled=1,
                payload_json=json.dumps({"portfolio_id": store.pid}), next_run_at=later),
            ScheduledTask(name="malformed", task_type=job.TASK_TYPE, enabled=1,
                payload_json="not-json", next_run_at=later),
        ]
        db.add_all(schedules)
        db.commit()
        ids = {schedule.name: schedule.id for schedule in schedules}
    updates = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE SCHEDULED_TASKS"):
            updates.append(statement)
    event.listen(store.engine, "before_cursor_execute", observe)
    try:
        with store.factory() as db:
            result = job.request_reconciliation_check(db, store.pid)
            assert set(result["advanced_schedule_ids"]) == {ids["global"], ids["mine"]}
            assert result["already_due_schedule_ids"] == [ids["already"]]
            assert not updates  # The helper owns neither flush nor commit.
            db.flush()
            count = len(updates)
            repeated = job.request_reconciliation_check(db, store.pid)
            assert repeated["reason"] == "ALREADY_DUE"
            assert not repeated["advanced_schedule_ids"]
            db.flush()
            assert len(updates) == count
            assert not list(db.scalars(select(AsyncTaskRecord)))
            db.commit()
        with store.factory() as db:
            assert db.get(ScheduledTask, ids["global"]).next_run_at == AT.replace(tzinfo=None)
            assert db.get(ScheduledTask, ids["mine"]).next_run_at == AT.replace(tzinfo=None)
            assert db.get(ScheduledTask, ids["already"]).next_run_at == earlier
            for name in ("disabled", "other", "trading", "malformed"):
                assert db.get(ScheduledTask, ids[name]).next_run_at == later
    finally:
        event.remove(store.engine, "before_cursor_execute", observe)


def test_event_check_uses_callers_transaction_and_respects_no_enabled_schedule(store):
    later = AT.replace(tzinfo=None) + timedelta(hours=1)
    with store.factory() as db:
        disabled = ScheduledTask(name="disabled-only", task_type=job.TASK_TYPE, enabled=0,
                                 payload_json="{}", next_run_at=later)
        db.add(disabled)
        db.commit()
        assert job.request_reconciliation_check(db, store.pid)["reason"] == "NO_ENABLED_RECONCILIATION_SCHEDULE"
        disabled.enabled = 1
        db.commit()
        result = job.request_reconciliation_check(db, store.pid)
        assert result["scheduled"] is True
        assert result["task_created"] is False
        db.rollback()
        assert db.get(ScheduledTask, disabled.id).next_run_at == later
