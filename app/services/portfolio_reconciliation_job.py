"""Persistent, evidence-idempotent account checks on the async task system.

The worker only reads trading evidence and asks the reconciliation service to
save its report. It never submits orders, repairs evidence or accepts coverage.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import threading
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select, update
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio
from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationCursor, PortfolioReconciliationDay
from app.models.scheduled_task import ScheduledTask
from app.schemas.async_task import AsyncTaskRead
from app.services import async_tasks
from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
from app.services.portfolio_reconciliation import latest_reconciliation_results, reconcile_trade_date
from app.services.portfolio_resume_service import get_reconciliation_queue


TASK_TYPE = "portfolio_reconciliation"
POLICY_VERSION = "portfolio-reconciliation-job-v1"
TERMINAL = {"done", "partial", "failed", "cancelled"}
_CREATE_LOCK = threading.RLock()
_PORTFOLIO_LOCKS: dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
RETRY_DELAYS_MINUTES = (1, 5, 15)
RETRY_ERROR_CODES = {"RECONCILIATION_TRANSIENT_READ_ERROR", "RECONCILIATION_DISPATCH_FAILED", "BACKEND_RESTART_INTERRUPTED"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _object(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _schedule_payload(raw: str | None) -> dict | None:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def _items(value: dict) -> list[dict]:
    rows = value.get("items")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _persistent_refs(report) -> dict:
    progression = report.progression or {}
    coverage = getattr(report, "coverage", None) or {}
    return {"persistent_report_id": getattr(report, "persistent_report_id", None)
                or coverage.get("persistent_report_id") or progression.get("current_report_id"),
            "persistent_report_version": getattr(report, "persistent_report_version", None)
                or coverage.get("persistent_report_version") or progression.get("report_version")}


def _date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValueError("trade_date must be a calendar date")
    return value if isinstance(value, date) else date.fromisoformat(value)


def _naive_now() -> datetime:
    return _now().astimezone(timezone.utc).replace(tzinfo=None)


def _work_day(db: Session, portfolio_id: int, target: date, *, create: bool = False, lock: bool = False):
    if "portfolio_reconciliation_days" not in inspect(db.connection()).get_table_names():
        return None
    statement = select(PortfolioReconciliationCursor).where(
        PortfolioReconciliationCursor.portfolio_id == portfolio_id,
        PortfolioReconciliationCursor.scope_key == "account")
    cursor = db.scalar(statement.with_for_update() if lock else statement)
    if cursor is None or target < cursor.coverage_start_trade_date:
        return None
    statement = select(PortfolioReconciliationDay).where(PortfolioReconciliationDay.cursor_id == cursor.id,
        PortfolioReconciliationDay.generation == cursor.generation, PortfolioReconciliationDay.target_trade_date == target)
    day = db.scalar(statement.with_for_update() if lock else statement)
    if day is None and create:
        day = PortfolioReconciliationDay(id="recon-day-" + _hash({"cursor": cursor.id, "generation": cursor.generation, "date": target})[:32],
            cursor_id=cursor.id, generation=cursor.generation, target_trade_date=target,
            requested_evidence_version=1, processed_evidence_version=0, attempt_count=0,
            work_state="queued", verdict="EVIDENCE_INCOMPLETE")
        db.add(day)
        db.flush()
    return day


def _work_fields(day) -> dict:
    if day is None:
        return {}
    return {"work_day_id": day.id, "requested_evidence_version": day.requested_evidence_version,
        "account_source_pending": bool(day.account_source_pending),
        "processed_evidence_version": day.processed_evidence_version, "attempt_count": day.attempt_count,
        "last_error_code": day.last_error_code,
        "retry_exhausted": day.last_error_code in RETRY_ERROR_CODES and day.attempt_count > len(RETRY_DELAYS_MINUTES),
        "next_check_at": day.next_check_at.replace(tzinfo=timezone.utc).isoformat() if day.next_check_at else None}


def _persist_plan_work(db: Session, items: list[dict], *, task_id: str | None = None) -> None:
    for item in sorted(items, key=lambda row: row["portfolio_id"]):
        target = _date(item.get("target_trade_date"))
        if target is None or not item.get("coverage"):
            continue
        db.scalar(select(Portfolio.id).where(Portfolio.id == item["portfolio_id"]).with_for_update())
        day = _work_day(db, item["portfolio_id"], target, create=True, lock=True)
        if day is None:
            continue
        if task_id:
            day.task_id = task_id
        if item.get("work_state") == "waiting_execution":
            day.work_state = "waiting_execution"
            day.next_check_at = datetime.fromisoformat(item["next_check_at"]).replace(tzinfo=None) if item.get("next_check_at") else _naive_now() + timedelta(minutes=5)


def _failure_checkpoint(db: Session, portfolio_id: int, target: date, task_id: str, code: str, *, expected_generation=None) -> dict:
    db.scalar(select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update())
    cursor = db.scalar(select(PortfolioReconciliationCursor).where(PortfolioReconciliationCursor.portfolio_id == portfolio_id,
        PortfolioReconciliationCursor.scope_key == "account").with_for_update())
    if cursor is not None and expected_generation is not None and cursor.generation != expected_generation:
        return {"stale_scope": True}
    day = _work_day(db, portfolio_id, target, create=True, lock=True)
    if day is None:
        return {}
    failures = day.attempt_count + 1 if day.last_error_code in RETRY_ERROR_CODES else 1
    day.attempt_count, day.last_error_code, day.task_id = failures, code, task_id
    day.verdict = "READ_ERROR"
    day.work_state = "waiting_data" if failures <= len(RETRY_DELAYS_MINUTES) else "needs_review"
    day.next_check_at = _naive_now() + timedelta(minutes=RETRY_DELAYS_MINUTES[failures - 1]) if failures <= len(RETRY_DELAYS_MINUTES) else None
    return _work_fields(day)


def _recover_work_after_restart(db: Session, portfolio_id: int | None) -> None:
    """Recover persisted requests only; no trading worker or report is replayed."""
    if "portfolio_reconciliation_days" not in inspect(db.connection()).get_table_names():
        return
    statement = select(PortfolioReconciliationDay, PortfolioReconciliationCursor).join(
        PortfolioReconciliationCursor, PortfolioReconciliationCursor.id == PortfolioReconciliationDay.cursor_id).where(
        PortfolioReconciliationDay.generation == PortfolioReconciliationCursor.generation,
        PortfolioReconciliationDay.requested_evidence_version > PortfolioReconciliationDay.processed_evidence_version,
        PortfolioReconciliationDay.work_state.in_(["queued", "running", "waiting_execution"]))
    if portfolio_id is not None:
        statement = statement.where(PortfolioReconciliationCursor.portfolio_id == portfolio_id)
    for day, cursor in db.execute(statement.order_by(PortfolioReconciliationCursor.portfolio_id, PortfolioReconciliationDay.target_trade_date)):
        task = db.get(AsyncTaskRecord, day.task_id) if day.task_id else None
        if task and (task.status == "cancelled" or task.cancel_requested):
            day.work_state, day.next_check_at = "cancelled", None
        elif task and task.status == "failed" and day.last_error_code not in RETRY_ERROR_CODES:
            _failure_checkpoint(db, cursor.portfolio_id, day.target_trade_date, task.id, "BACKEND_RESTART_INTERRUPTED")


def _cancel_pending_work(db: Session, task_id: str, items: list[dict]) -> None:
    for item in sorted(items, key=lambda row: row["portfolio_id"]):
        target = _date(item.get("target_trade_date"))
        if target is None:
            continue
        db.scalar(select(Portfolio.id).where(Portfolio.id == item["portfolio_id"]).with_for_update())
        day = _work_day(db, item["portfolio_id"], target, lock=True)
        if day and day.task_id == task_id and day.requested_evidence_version > day.processed_evidence_version:
            newer_demand = day.requested_evidence_version > item.get("requested_evidence_version", 1)
            day.work_state, day.next_check_at = ("queued", _naive_now()) if newer_demand else ("cancelled", None)
def _coverage(db: Session, portfolio_id: int, *, current_read: bool = False) -> tuple[dict | None, str | None]:
    """Missing deployment components are explicit unknowns, never legacy passes."""
    tables = set(inspect(db.connection()).get_table_names())
    if not {"portfolio_account_baselines", "portfolio_reconciliation_cursors"} <= tables:
        return None, "COVERAGE_STORAGE_UNAVAILABLE"
    try:
        from app.services.portfolio_reconciliation_coverage import CoverageError, get_active_coverage
    except ImportError:
        return None, "COVERAGE_READER_UNAVAILABLE"
    try:
        value = get_active_coverage(db, portfolio_id, current_read=current_read)
    except CoverageError as exc:
        return None, exc.code
    if value is None:
        return None, "HISTORICAL_COVERAGE_UNKNOWN"
    return dict(value), None


def _action(code: str, label: str, portfolio_id: int, trade_date: date | None = None) -> list[dict]:
    return [{"code": code, "label": label, "enabled": True, "portfolio_id": portfolio_id,
             "trade_date": trade_date.isoformat() if trade_date else None, "requires_confirmation": False}]


def _unknown(portfolio_id: int, code: str, reason: str, *, coverage=None, calendar=None) -> dict:
    coverage_issue = "COVERAGE" in code or "BASELINE" in code
    return {"portfolio_id": portfolio_id, "target_trade_date": None,
            "verdict": code, "work_state": "waiting_data", "reason_codes": [code], "reason": reason,
            "actions": _action("REVIEW_COVERAGE" if coverage_issue else "INSPECT_CALENDAR",
                               "核实账户基线与覆盖范围" if coverage_issue else "核实交易所日历", portfolio_id),
            "coverage": coverage, "calendar": calendar, "report_id": None, "report_version": None,
            "source_fingerprint": _hash({"code": code, "coverage": coverage, "calendar": calendar}),
            "can_reconcile": False}


def _plan_portfolio(db: Session, portfolio_id: int, requested: date | None, at: datetime, *, current_read: bool = False) -> dict:
    coverage, error = _coverage(db, portfolio_id, current_read=current_read)
    if error:
        return _unknown(portfolio_id, error, "历史核验覆盖尚未建立或不可读取，自动作业不能沿用旧游标放行")
    frozen = {key: coverage.get(key) for key in (
        "cursor_id", "generation", "baseline_id", "coverage_start_trade_date", "calendar_version")}
    if not all(frozen.get(key) is not None for key in ("cursor_id", "generation", "baseline_id", "coverage_start_trade_date")):
        return _unknown(portfolio_id, "HISTORICAL_COVERAGE_UNKNOWN", "当前覆盖缺少可核验的代次或基线", coverage=frozen)
    calendar = resolve_inspection_calendar(db, at=at, current_read=current_read)
    calendar_fact = {key: calendar.get(key) for key in ("status", "source", "version", "expected_trade_date", "reason")}
    if calendar.get("status") != "PASSED" or not calendar.get("expected_trade_date"):
        return _unknown(portfolio_id, "CALENDAR_UNKNOWN", "交易所日历未知，无法推导应核对日期", coverage=frozen, calendar=calendar_fact)
    local_day = at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    queue = get_reconciliation_queue(db, portfolio_id, today=local_day)
    if queue.get("calendar_status") not in {"KNOWN", "PASSED"}:
        return _unknown(portfolio_id, "CALENDAR_UNKNOWN", "覆盖范围内交易日历尚未核实，不能跳过未知日期", coverage=frozen, calendar=calendar_fact)
    start = _date(coverage["coverage_start_trade_date"])
    verified = _date(coverage.get("verified_through_trade_date"))
    target = requested or _date(queue.get("target_trade_date"))
    # Independently enforce continuity even if a queue adapter is stale.
    earliest_query = select(MarketCalendar.trade_date).where(
        MarketCalendar.is_trading_day == 1,
        MarketCalendar.trade_date > verified if verified else MarketCalendar.trade_date >= start,
    ).order_by(MarketCalendar.trade_date).limit(1)
    earliest = db.scalar(earliest_query.with_for_update() if current_read else earliest_query)
    if target is None:
        target = earliest
    if target is None:
        return _unknown(portfolio_id, "CALENDAR_UNKNOWN", "未找到覆盖范围内下一真实交易日", coverage=frozen, calendar=calendar_fact)
    target_query = select(MarketCalendar.is_trading_day).where(MarketCalendar.trade_date == target)
    is_trade_day = db.scalar(target_query.with_for_update() if current_read else target_query)
    if target < start or is_trade_day != 1:
        result = _unknown(portfolio_id, "TARGET_OUTSIDE_COVERAGE", "请求日期不属于本代已接受的交易日覆盖范围", coverage=frozen, calendar=calendar_fact)
        result["requested_trade_date"] = target.isoformat()
        return result
    if earliest is not None and target > earliest:
        result = _unknown(portfolio_id, "PREVIOUS_DATE_UNVERIFIED", "前序交易日尚未完成，不能从后日开始核对", coverage=frozen, calendar=calendar_fact)
        result.update(target_trade_date=earliest.isoformat(), requested_trade_date=target.isoformat(),
                      actions=_action("REVIEW_RECONCILIATION_QUEUE", "查看前序待核对日期", portfolio_id, earliest))
        return result
    day = _work_day(db, portfolio_id, target, lock=current_read)
    control = _work_fields(day)
    retry_wait = bool(day and day.last_error_code in RETRY_ERROR_CODES and (
        control["retry_exhausted"] or day.next_check_at and day.next_check_at > at.astimezone(timezone.utc).replace(tzinfo=None)))
    if retry_wait or day and day.work_state == "cancelled":
        return {"portfolio_id": portfolio_id, "target_trade_date": target.isoformat(), "coverage": frozen, "calendar": calendar_fact,
            "verdict": "READ_ERROR" if retry_wait else day.verdict, "work_state": day.work_state,
            "reason_codes": [day.last_error_code] if day.last_error_code else ["CHECK_CANCELLED"],
            "reason": "自动重试已用尽，请核查来源后手动重新检查" if control.get("retry_exhausted") else
                      "读取失败已记录，等待下次退避时间" if retry_wait else "本次核对工作已取消，已保存报告保留",
            "actions": _action("RETRY_RECONCILIATION", "核查来源并重新检查", portfolio_id, target),
            "can_reconcile": False, "attempted": False, **control,
            "source_fingerprint": _hash({"coverage": frozen, "target": target, "control": control, "due": False})}
    try:
        report = reconcile_trade_date(db, portfolio_id, target, persist=False, now=at, current_read=current_read)
    except Exception as exc:
        return {"portfolio_id": portfolio_id, "target_trade_date": target.isoformat(), "coverage": frozen, "calendar": calendar_fact,
            "verdict": "READ_ERROR", "work_state": "waiting_data", "reason_codes": ["RECONCILIATION_SOURCE_READ_ERROR"],
            "reason": "当前日期来源读取失败，按持久重试规则重新读取", "error_type": type(exc).__name__,
            "actions": _action("RETRY_RECONCILIATION", "重新读取核对资料", portfolio_id, target),
            "report_id": None, "report_version": None, "can_reconcile": True, **control,
            "source_fingerprint": _hash({"coverage": frozen, "target": target, "requested": control.get("requested_evidence_version", 1),
                "attempt_count": control.get("attempt_count", 0), "error_type": type(exc).__name__, "due": True})}
    expected = _date(calendar["expected_trade_date"])
    # This is a T+1 account check. A daily bar becoming due at 15:00 is
    # unrelated to the evening decision and the next session's execution.
    # Never turn today's not-yet-created run into a reconciliation pause.
    waiting = target >= local_day or target > expected
    source_read_error = "READ_ERROR" in report.reason_codes or any(
        row.get("query_status") == "READ_ERROR" for row in report.activity_evidence)
    result = {"portfolio_id": portfolio_id, "target_trade_date": target.isoformat(),
              "coverage": frozen, "calendar": calendar_fact,
              "verdict": "NOT_DUE" if waiting else "READ_ERROR" if source_read_error else report.verdict,
              "work_state": "waiting_execution" if waiting else report.work_state,
              "reason_codes": ["NOT_DUE"] if waiting else report.reason_codes,
              "reason": "目标交易日尚未进入应核对范围，等待执行完成" if waiting else
                        ("；".join(report.reason_codes) or "本日核对证据已读取"),
              "actions": _action("VIEW_EXECUTION_PROGRESS", "查看执行进度", portfolio_id, target) if waiting else report.actions,
              "report_id": report.report_id, "report_version": report.report_version,
              **_persistent_refs(report),
              "next_check_at": report.next_check_at, "can_reconcile": not waiting,
              "source_fingerprint": report.report_version if not day or day.requested_evidence_version == 1 and not day.last_error_code else
                  _hash({"report": report.report_version, "requested": day.requested_evidence_version,
                         "retry_attempt": day.attempt_count if day.last_error_code in RETRY_ERROR_CODES else 0}),
              **control}
    if report.next_check_at:
        result["next_check_at"] = report.next_check_at
    return result


def build_portfolio_reconciliation_job_plan(db: Session, *, portfolio_id: int | None = None,
        trade_date: str | date | None = None, now: datetime | None = None) -> dict:
    """Read-only plan. Unknowns stay actionable results without invented dates."""
    at = now or _now()
    at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
    requested = _date(trade_date)
    with db.no_autoflush:
        if portfolio_id is not None:
            if db.get(Portfolio, portfolio_id) is None:
                raise ValueError(f"Portfolio {portfolio_id} not found")
            ids = [portfolio_id]
        else:
            ids = list(db.scalars(select(Portfolio.id).order_by(Portfolio.id)))
        items = []
        for pid in ids:
            try:
                items.append(_plan_portfolio(db, pid, requested, at))
            except Exception as exc:
                items.append({"portfolio_id": pid, "target_trade_date": None,
                    "verdict": "READ_ERROR", "work_state": "waiting_data", "reason_codes": ["RECONCILIATION_PLAN_READ_FAILED"],
                    "reason": "当前核对资料读取失败，请重试此组合", "error_type": type(exc).__name__,
                    "actions": _action("RETRY_RECONCILIATION", "重新读取核对资料", pid),
                    "source_fingerprint": _hash({"portfolio_id": pid, "error_type": type(exc).__name__}),
                    "can_reconcile": False})
    identity = {"policy_version": POLICY_VERSION, "portfolio_id": portfolio_id,
                "requested_trade_date": requested, "items": [{key: item.get(key) for key in (
                    "portfolio_id", "target_trade_date", "coverage", "calendar", "source_fingerprint", "verdict")}
                    for item in items]}
    return {"policy_version": POLICY_VERSION, "portfolio_id": portfolio_id,
            "requested_trade_date": requested.isoformat() if requested else None,
            "generated_at": at.isoformat(), "items": items, "request_fingerprint": _hash(identity)}


def create_portfolio_reconciliation_task(*, portfolio_id: int | None = None,
        trade_date: str | date | None = None, schedule_id: int | None = None,
        source_request_id: str | None = None) -> AsyncTaskRead:
    """Create once per source evidence; a failed execution gets a new attempt."""
    with _CREATE_LOCK:
        with get_session_local()() as db:
            from app.services.portfolio_reconciliation_events import consume_source_events
            consume_source_events(db, portfolio_id=portfolio_id)
            _recover_work_after_restart(db, portfolio_id)
            if schedule_id is None and portfolio_id is not None:
                for day, cursor in db.execute(select(PortfolioReconciliationDay, PortfolioReconciliationCursor).join(
                    PortfolioReconciliationCursor, PortfolioReconciliationCursor.id == PortfolioReconciliationDay.cursor_id).where(
                    PortfolioReconciliationCursor.portfolio_id == portfolio_id,
                    PortfolioReconciliationDay.generation == PortfolioReconciliationCursor.generation,
                    PortfolioReconciliationDay.work_state.in_(["needs_review", "cancelled"]))):
                    if day.work_state == "cancelled" or day.last_error_code in RETRY_ERROR_CODES:
                        day.requested_evidence_version += 1
                        day.work_state, day.last_error_code, day.attempt_count, day.next_check_at = "queued", None, 0, _naive_now()
            db.commit()
            plan = build_portfolio_reconciliation_job_plan(db, portfolio_id=portfolio_id, trade_date=trade_date)
            _persist_plan_work(db, plan["items"])
            db.commit()
            base = plan["request_fingerprint"]
            attempt = 1
            previous = None
            for row in db.scalars(select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == TASK_TYPE)
                    .order_by(AsyncTaskRecord.created_at.desc(), AsyncTaskRecord.id.desc())):
                payload = _object(row.payload_json)
                if payload.get("base_fingerprint") == base:
                    if previous is None or payload.get("attempt", 1) > _object(previous.payload_json).get("attempt", 1):
                        previous = row
            if previous is not None:
                if previous.status not in {"failed", "partial"}:
                    from app.services.portfolio_recheck_requests import bind_task
                    bind_task(db, source_request_id, previous.id)
                    return async_tasks._task_to_read(previous)
                attempt = int(_object(previous.payload_json).get("attempt", 1)) + 1
        fingerprint = _hash({"base_fingerprint": base, "attempt": attempt})
        task_id = _hash({"task_type": TASK_TYPE, "request_fingerprint": fingerprint})[:32]
        payload = {**plan, "base_fingerprint": base, "attempt": attempt, "schedule_id": schedule_id,
                   "request_fingerprint": fingerprint}
        # Persist the exact intended identity BEFORE task creation/worker start.
        # A lost response or crash in between is recovered by this ID, never
        # by whichever task happens to be latest. Replayed receipts do not dispatch.
        if source_request_id is not None:
            from app.services.portfolio_recheck_requests import bind_task
            with get_session_local()() as db:
                bind_task(db, source_request_id, task_id)
        task, created = async_tasks.create_async_task_once(TASK_TYPE, payload,
            task_id=task_id, request_fingerprint=fingerprint)
        if created:
            with get_session_local()() as db:
                _persist_plan_work(db, plan["items"], task_id=task.id)
                db.commit()
            try:
                async_tasks._start_worker(task.id, run_portfolio_reconciliation_worker)
            except Exception:
                with get_session_local()() as db:
                    for item in plan["items"]:
                        if item.get("target_trade_date"):
                            _failure_checkpoint(db, item["portfolio_id"], _date(item["target_trade_date"]), task.id, "RECONCILIATION_DISPATCH_FAILED",
                                expected_generation=(item.get("coverage") or {}).get("generation"))
                    db.commit()
                _publish(task.id, status="failed", stage="dispatch_failed", message="自动核对作业启动失败，请重试",
                         finished_at=_now().replace(tzinfo=None))
                raise
        return task


def _publish(task_id: str, **values) -> bool:
    """Protect the whole terminal record, including its result, from late workers."""
    with get_session_local()() as db:
        values["updated_at"] = _now().replace(tzinfo=None)
        if values.get("status") in TERMINAL:
            values["is_terminal_locked"] = 1
        changed = db.execute(update(AsyncTaskRecord).where(
            AsyncTaskRecord.id == task_id, AsyncTaskRecord.status.not_in(TERMINAL),
            (AsyncTaskRecord.is_terminal_locked == 0) | AsyncTaskRecord.is_terminal_locked.is_(None),
            (AsyncTaskRecord.cancel_requested == 0) | AsyncTaskRecord.cancel_requested.is_(None),
        ).values(**values)).rowcount
        db.commit()
        return bool(changed)


def _is_stopped(task_id: str) -> bool:
    if async_tasks.is_worker_stop_requested(task_id):
        return True
    with get_session_local()() as db:
        row = db.get(AsyncTaskRecord, task_id)
        return row is None or row.status in TERMINAL or bool(row.cancel_requested) or bool(row.is_terminal_locked)


def _portfolio_lock(portfolio_id: int):
    # SQLite has no SELECT FOR UPDATE; the process lock supplements the DB
    # row lock. Production databases serialize distinct jobs on Portfolio.
    with _LOCKS_GUARD:
        return _PORTFOLIO_LOCKS.setdefault(portfolio_id, threading.RLock())


def _run_item(task_id: str, item: dict, correlation_id: str | None) -> dict:
    pid = item["portfolio_id"]
    with _portfolio_lock(pid), get_session_local()() as db:
        p = db.scalar(select(Portfolio).where(Portfolio.id == pid).with_for_update()
                      .execution_options(populate_existing=True))
        if p is None:
            raise ValueError("portfolio_removed")
        current = _plan_portfolio(db, pid, _date(item.get("target_trade_date")), _now(), current_read=True)
        original_scope, current_scope = item.get("coverage"), current.get("coverage")
        scope_keys = ("cursor_id", "generation", "baseline_id", "coverage_start_trade_date")
        if original_scope and current_scope and _hash({key: original_scope.get(key) for key in scope_keys}) != _hash({key: current_scope.get(key) for key in scope_keys}):
            db.rollback()
            return {**current, "verdict": "COVERAGE_GENERATION_CHANGED", "work_state": "needs_review",
                "reason": "任务确认后账户覆盖范围已变化，旧任务不写入新代报告", "can_reconcile": False, "attempted": False}
        if not current["can_reconcile"]:
            db.rollback()
            return {**current, "reused_report": False}
        if current["verdict"] == "READ_ERROR":
            raise RuntimeError("reconciliation_source_read_error")
        target = _date(current["target_trade_date"])
        day = _work_day(db, pid, target, create=True, lock=True)
        frozen_requested = day.requested_evidence_version if day else None
        if day:
            day.work_state, day.task_id = "running", task_id
        latest = latest_reconciliation_results(db, pid).get(target, {})
        coverage = current["coverage"]
        same_generation = (latest.get("coverage_generation") == coverage["generation"]
                           and latest.get("baseline_id") == coverage["baseline_id"])
        if same_generation and latest.get("report_version") == current["report_version"] and not (
                day and day.requested_evidence_version > day.processed_evidence_version):
            db.rollback()
            progression = latest.get("progression") or {}
            return {**current, "reused_report": True, "audit_event_id": latest.get("audit_event_id"),
                    "persistent_report_id": latest.get("persistent_report_id") or progression.get("current_report_id"),
                    "persistent_report_version": latest.get("persistent_report_version") or progression.get("report_version")}
        report = reconcile_trade_date(db, pid, target, persist=True, now=_now(),
            operator_id="system:portfolio-reconciliation", correlation_id=correlation_id, current_read=True,
            expected_requested_evidence_version=frozen_requested)
        # Re-read cancellation using a current row read before committing this
        # item's report. A cancelled task keeps both its terminal state/result.
        task = db.get(AsyncTaskRecord, task_id, with_for_update=True, populate_existing=True)
        if task is None or task.status in TERMINAL or task.cancel_requested or task.is_terminal_locked:
            db.rollback()
            raise InterruptedError("reconciliation_cancelled")
        if day:
            day.processed_evidence_version = max(day.processed_evidence_version, frozen_requested)
            pending_new = day.requested_evidence_version > day.processed_evidence_version
            day.work_state = "queued" if pending_new else report.work_state
            day.verdict, day.attempt_count, day.last_error_code = report.verdict, 0, None
            day.next_check_at = _naive_now() if pending_new else datetime.fromisoformat(report.next_check_at).replace(tzinfo=None) if report.next_check_at else None
            if day.work_state == "waiting_execution" and day.next_check_at is None:
                day.next_check_at = _naive_now() + timedelta(minutes=5)
        db.commit()
        return {**current, "verdict": report.verdict, "work_state": report.work_state,
                "reason_codes": report.reason_codes, "actions": report.actions,
                "report_id": report.report_id, "report_version": report.report_version,
                **_persistent_refs(report),
                "audit_event_id": report.audit.event_id if report.audit else None,
                "differences": [asdict(diff) for diff in report.differences],
                "missing_evidence": report.missing_evidence, "progression": report.progression,
                "next_check_at": report.next_check_at, "reused_report": False,
                "attempted": True, **_work_fields(day)}


def run_portfolio_reconciliation_worker(task_id: str) -> None:
    with get_session_local()() as db:
        row = db.get(AsyncTaskRecord, task_id)
        if row is None or row.task_type != TASK_TYPE:
            return
        payload, correlation_id = _object(row.payload_json), row.correlation_id
        if row.status != "queued" or row.cancel_requested or row.is_terminal_locked:
            if row.status == "cancelled" or row.cancel_requested:
                _cancel_pending_work(db, task_id, payload.get("items", []))
                db.commit()
            return
        claimed = db.execute(update(AsyncTaskRecord).where(AsyncTaskRecord.id == task_id,
            AsyncTaskRecord.status == "queued", AsyncTaskRecord.is_terminal_locked == 0,
            (AsyncTaskRecord.cancel_requested == 0) | AsyncTaskRecord.cancel_requested.is_(None)).values(
                status="running", stage="reconciling", started_at=_now().replace(tzinfo=None),
                heartbeat_at=_now().replace(tzinfo=None))).rowcount
        db.commit()
        if not claimed:
            return
    items, results = payload.get("items", []), []
    for index, item in enumerate(items):
        if _is_stopped(task_id):
            with get_session_local()() as cancelled_db:
                task = cancelled_db.get(AsyncTaskRecord, task_id)
                if task and (task.status == "cancelled" or task.cancel_requested):
                    _cancel_pending_work(cancelled_db, task_id, items[index:])
                    cancelled_db.commit()
            return
        try:
            result = _run_item(task_id, item, correlation_id)
        except InterruptedError:
            with get_session_local()() as cancelled_db:
                _cancel_pending_work(cancelled_db, task_id, items[index:])
                cancelled_db.commit()
            return
        except Exception as exc:
            work = {}
            if item.get("target_trade_date"):
                with get_session_local()() as error_db:
                    work = _failure_checkpoint(error_db, item["portfolio_id"], _date(item["target_trade_date"]), task_id,
                        "RECONCILIATION_TRANSIENT_READ_ERROR", expected_generation=(item.get("coverage") or {}).get("generation"))
                    error_db.commit()
            result = {"portfolio_id": item["portfolio_id"], "target_trade_date": item.get("target_trade_date"),
                "verdict": "READ_ERROR", "work_state": "waiting_data", "reason_codes": ["RECONCILIATION_ITEM_FAILED"],
                "reason": "此组合核对失败，其余组合继续执行，请重试此组合", "error_type": type(exc).__name__,
                "actions": _action("RETRY_RECONCILIATION", "重试此组合核对", item["portfolio_id"]),
                "report_id": None, "report_version": None, "attempted": True, **work}
        results.append(result)
        if not _publish(task_id, total=len(items), processed=len(results), percent=100 * len(results) / max(1, len(items)),
                heartbeat_at=_now().replace(tzinfo=None), current_item=str(item["portfolio_id"]),
                result_json=_json({"items": results, "policy_version": POLICY_VERSION})):
            return
    failures = sum(item["verdict"] == "READ_ERROR" and item.get("attempted", True) for item in results)
    status = "failed" if failures and failures == len(results) else "partial" if failures else "done"
    result = {"items": results, "policy_version": POLICY_VERSION, "portfolio_count": len(results),
              "passed_count": sum(item["verdict"] == "PASSED" for item in results), "error_count": failures,
              "waiting_count": sum(item.get("work_state") in {"waiting_execution", "waiting_data"} for item in results),
              "difference_count": sum(item["verdict"] == "DIFF_FOUND" for item in results)}
    _publish(task_id, status=status, stage=status, percent=100, total=len(items), processed=len(results),
             ok_count=result["passed_count"], failed_count=failures, result_json=_json(result),
             message="自动核对已完成；请按逐组合结论处理差异、等待项或未知证据",
             finished_at=_now().replace(tzinfo=None))


def _includes_portfolio(payload: dict, result: dict, portfolio_id: int, *, global_scope: bool = False) -> bool:
    ids = payload.get("portfolio_ids")
    ids = ids if isinstance(ids, list) else []
    if payload.get("portfolio_id") == portfolio_id or portfolio_id in ids:
        return True
    scoped_items = _items(payload) + _items(result)
    if any(item.get("portfolio_id") == portfolio_id for item in scoped_items):
        return True
    return global_scope and payload.get("portfolio_id") is None and not payload.get("portfolio_ids") and not scoped_items


def request_reconciliation_check(db: Session, portfolio_id: int, *, trade_date: date | None = None, register_demand: bool = True) -> dict:
    """Coalesce an account event into an enabled reconciliation schedule.

    The caller owns commit. No async task or trading schedule is dispatched,
    and an already-due schedule receives no second UPDATE.
    """
    at = _now().astimezone(timezone.utc).replace(tzinfo=None)
    advanced, already_due = [], []
    requested_day_ids = []
    with db.no_autoflush:
        if register_demand and "portfolio_reconciliation_days" in inspect(db.connection()).get_table_names():
            db.scalar(select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update())
            cursor = db.scalar(select(PortfolioReconciliationCursor).where(PortfolioReconciliationCursor.portfolio_id == portfolio_id,
                PortfolioReconciliationCursor.scope_key == "account").with_for_update())
            if cursor is not None:
                calendar = resolve_inspection_calendar(db, at=_now())
                known = calendar.get("status") == "PASSED"
                actual_dates = set(calendar.get("trading_dates", [])) if known else set()
                target = trade_date
                if target is None:
                    target = next((day for day in sorted(actual_dates) if day > cursor.verified_through_trade_date), None) if cursor.verified_through_trade_date else cursor.coverage_start_trade_date
                dates = {target} if target in actual_dates else set()
                dates.update(db.scalars(select(PortfolioReconciliationDay.target_trade_date).where(
                    PortfolioReconciliationDay.cursor_id == cursor.id, PortfolioReconciliationDay.generation == cursor.generation,
                    PortfolioReconciliationDay.requested_evidence_version > PortfolioReconciliationDay.processed_evidence_version)))
                for target_date in sorted(dates):
                    day = _work_day(db, portfolio_id, target_date, create=False, lock=True)
                    if day is None:
                        day = _work_day(db, portfolio_id, target_date, create=True, lock=True)
                    else:
                        day.requested_evidence_version += 1
                    day.work_state = "queued"
                    day.next_check_at, day.last_error_code, day.attempt_count = at, None, 0
                    requested_day_ids.append(day.id)
        if "scheduled_tasks" not in inspect(db.connection()).get_table_names():
            return {"portfolio_id": portfolio_id, "requested_at": at.isoformat(),
                    "advanced_schedule_ids": [], "already_due_schedule_ids": [],
                    "scheduled": False, "task_created": False, "requested_day_ids": requested_day_ids,
                    "reason": "RECONCILIATION_SCHEDULE_STORAGE_UNAVAILABLE"}
        for schedule in db.scalars(select(ScheduledTask).where(
                ScheduledTask.task_type == TASK_TYPE, ScheduledTask.enabled == 1,
                ).order_by(ScheduledTask.id).with_for_update()):
            payload = _schedule_payload(schedule.payload_json)
            if payload is None or not _includes_portfolio(payload, {}, portfolio_id, global_scope=True):
                continue
            next_run = schedule.next_run_at
            if next_run is not None and next_run.tzinfo is not None:
                next_run = next_run.astimezone(timezone.utc).replace(tzinfo=None)
            if next_run is not None and next_run <= at:
                already_due.append(schedule.id)
                continue
            schedule.next_run_at = at
            advanced.append(schedule.id)
    return {"portfolio_id": portfolio_id, "requested_at": at.isoformat(),
            "advanced_schedule_ids": advanced, "already_due_schedule_ids": already_due,
            "requested_day_ids": requested_day_ids,
            "scheduled": bool(advanced or already_due), "task_created": False,
            "reason": "ALREADY_DUE" if already_due and not advanced else "CHECK_SCHEDULE_ADVANCED" if advanced
                      else "NO_ENABLED_RECONCILIATION_SCHEDULE"}


def _scoped_item(item: dict, portfolio_id: int) -> dict:
    """Public job summary, rather than the worker's opaque internal envelope."""
    fields = {"portfolio_id", "target_trade_date", "requested_trade_date", "verdict", "work_state", "reason",
              "report_id", "report_version", "persistent_report_id", "persistent_report_version",
              "next_check_at", "audit_event_id", "can_reconcile", "reused_report", "source_fingerprint", "error_type",
              "work_day_id", "requested_evidence_version", "processed_evidence_version", "attempt_count", "last_error_code", "retry_exhausted", "attempted"}
    scoped = {key: value for key, value in item.items() if key in fields
              and (value is None or isinstance(value, (str, int, float, bool)))}
    scoped["portfolio_id"] = portfolio_id
    codes = item.get("reason_codes")
    scoped["reason_codes"] = [code for code in codes if isinstance(code, str)] if isinstance(codes, list) else []
    action_fields = {"portfolio_id", "code", "label", "enabled", "trade_date", "requires_confirmation"}
    actions = item.get("actions")
    scoped["actions"] = [{key: value for key, value in action.items() if key in action_fields
                           and (value is None or isinstance(value, (str, int, float, bool)))}
                         for action in actions if isinstance(action, dict) and action.get("portfolio_id") == portfolio_id
                        ] if isinstance(actions, list) else []
    progression_fields = {"can_advance", "verified_through", "verified_through_trade_date", "first_blocked_trade_date",
                          "current_report_id", "report_version", "evidence_version", "generation", "baseline_id",
                          "coverage_source", "historical_coverage_unknown", "idempotent"}
    progression = item.get("progression")
    if isinstance(progression, dict):
        scoped["progression"] = {key: value for key, value in progression.items() if key in progression_fields
                                  and (value is None or isinstance(value, (str, int, float, bool)))}
    return scoped


def _scoped_errors(raw: str | None, portfolio_id: int) -> list[dict]:
    try:
        errors = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(errors, list):
        return []
    fields = {"portfolio_id", "code", "error_code", "severity", "category", "title_zh", "detail_zh", "retryable"}
    return [{key: value for key, value in error.items() if key in fields
             and (value is None or isinstance(value, (str, int, float, bool)))}
            for error in errors if isinstance(error, dict) and error.get("portfolio_id") == portfolio_id]


def _job_portfolio_view(row: AsyncTaskRecord, payload: dict, result: dict, portfolio_id: int) -> dict:
    """Never return batch payloads, recovery checkpoints or unscoped errors.

    Even a task that includes this portfolio may hold other accounts' evidence
    in its result, message or cancellation checkpoint. Only explicitly scoped
    result/error objects and enumerated lifecycle metadata are public here.
    """
    items = [_scoped_item(item, portfolio_id) for item in _items(result) if item.get("portfolio_id") == portfolio_id]
    errors = _scoped_errors(row.errors_json, portfolio_id)
    failed = sum(item.get("verdict") == "READ_ERROR" for item in items)
    passed = sum(item.get("verdict") == "PASSED" for item in items)
    waiting = sum(item.get("work_state") in {"waiting_execution", "waiting_data"} for item in items)
    lifecycle = {key: getattr(row, key) for key in (
        "id", "task_type", "status", "created_at", "started_at", "finished_at", "updated_at", "heartbeat_at")}
    for key, value in list(lifecycle.items()):
        if isinstance(value, datetime):
            lifecycle[key] = (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()
    known_stages = {"queued", "running", "reconciling", "done", "partial", "failed", "cancelled", "dispatch_failed"}
    stage = row.stage if row.stage in known_stages else row.status
    message = next((item["reason"] for item in items if isinstance(item.get("reason"), str) and item["reason"]), None)
    message = message or {"queued": "当前组合等待自动核对", "running": "当前组合自动核对作业执行中",
        "done": "请查看当前组合的核对结论", "partial": "请查看当前组合的核对结论与待处理项",
        "failed": "当前组合核对作业失败，请查看本组合材料", "cancelled": "当前组合核对作业已取消"}.get(row.status, "当前组合核对状态待核实")
    attempt = payload.get("attempt", 1)
    schedule_id = payload.get("schedule_id")
    scoped_result = {"portfolio_id": portfolio_id, "items": items, "portfolio_count": 1,
                     "passed_count": passed, "error_count": failed, "waiting_count": waiting,
                     "difference_count": sum(item.get("verdict") == "DIFF_FOUND" for item in items)}
    return {**lifecycle, "portfolio_id": portfolio_id, "stage": stage, "message": message,
            "percent": 100.0 if items else 0.0, "total": max(1, len(items)), "processed": len(items),
            "ok_count": passed, "failed_count": failed, "items": items, "result": scoped_result,
            "errors": errors, "error_code": (errors[0].get("code") or errors[0].get("error_code")) if errors else None,
            "attempt": attempt if isinstance(attempt, int) and attempt > 0 else 1,
            "schedule_id": schedule_id if isinstance(schedule_id, int) else None,
            "cancel_requested": bool(row.cancel_requested), "is_terminal_locked": bool(row.is_terminal_locked)}


def get_portfolio_reconciliation_jobs(db: Session, portfolio_id: int, limit: int = 20) -> dict:
    """Snapshot only: no task patrol, stale expiration, flush or schedule edits."""
    limit = max(1, min(int(limit), 100))
    schedules, jobs, work_items = [], [], []
    with db.no_autoflush:
        tables = set(inspect(db.connection()).get_table_names())
        if "scheduled_tasks" in tables:
            for row in db.scalars(select(ScheduledTask).where(ScheduledTask.task_type == TASK_TYPE).order_by(ScheduledTask.id)):
                payload = _schedule_payload(row.payload_json)
                if payload is not None and _includes_portfolio(payload, {}, portfolio_id, global_scope=True):
                    schedules.append({"id": row.id, "name": row.name, "enabled": bool(row.enabled),
                        "frequency": row.frequency, "time_of_day": row.time_of_day, "timezone": row.timezone,
                        "interval_minutes": row.interval_minutes, "weekdays": json.loads(row.weekdays_json or "[]"),
                        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
                        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
                        "last_status": row.last_status, "last_task_id": row.last_task_id,
                        "last_error": None, "has_error": bool(row.last_error)})
        if "async_tasks" in tables:
            # Filter after parsing every candidate's actual scope, before limit.
            # A busy unrelated portfolio cannot hide this portfolio's history.
            for row in db.scalars(select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == TASK_TYPE)
                    .order_by(AsyncTaskRecord.created_at.desc(), AsyncTaskRecord.id.desc())):
                payload, result = _object(row.payload_json), _object(row.result_json)
                if not _includes_portfolio(payload, result, portfolio_id):
                    continue
                jobs.append(_job_portfolio_view(row, payload, result, portfolio_id))
                if len(jobs) >= limit:
                    break
        if "portfolio_reconciliation_days" in tables:
            for day in db.scalars(select(PortfolioReconciliationDay).join(PortfolioReconciliationCursor,
                PortfolioReconciliationCursor.id == PortfolioReconciliationDay.cursor_id).where(
                PortfolioReconciliationCursor.portfolio_id == portfolio_id,
                PortfolioReconciliationDay.generation == PortfolioReconciliationCursor.generation).order_by(
                PortfolioReconciliationDay.target_trade_date).limit(100)):
                work_items.append({"trade_date": day.target_trade_date.isoformat(), "work_state": day.work_state,
                    "verdict": day.verdict, "task_id": day.task_id, **_work_fields(day)})
    from app.services.portfolio_reconciliation_events import pending_source_events
    with db.no_autoflush:
        source_events = pending_source_events(db, portfolio_id)
        source_events["waiting_for_coverage"] = not bool("portfolio_reconciliation_cursors" in tables and db.scalar(
            select(PortfolioReconciliationCursor.id).where(PortfolioReconciliationCursor.portfolio_id == portfolio_id)))
    return {"portfolio_id": portfolio_id, "schedules": schedules, "jobs": jobs, "work_items": work_items,
        "source_events": source_events}
