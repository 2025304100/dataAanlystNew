"""Observable portfolio execution using the existing in-process task worker."""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time, timezone
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.decision_engine import DecisionRun, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.services import async_tasks
from app.services.decision_schedule_gate import SHANGHAI
from app.services.portfolio_state_machine import _get_status, transition_portfolio_state
from app.services.portfolio_status import resolve_composite_status

_DISPATCH_LOCK = threading.RLock()
_ACTIVE_TASK_IDS: set[str] = set()
TASK_TYPE = "portfolio_auto_simulation"
logger = logging.getLogger(__name__)


def portfolio_execution_in_progress(db: Session, portfolio_id: int) -> bool:
    rows = db.scalars(select(AsyncTaskRecord).where(
        AsyncTaskRecord.task_type.in_((TASK_TYPE, "portfolio_resume")),
        AsyncTaskRecord.status.in_(("queued", "running")),
    ).execution_options(populate_existing=True))
    for task in rows:
        payload = json.loads(task.payload_json or "{}")
        if payload.get("portfolio_id") == portfolio_id and (
            task.status == "running" or task.id in _ACTIVE_TASK_IDS or async_tasks.is_worker_running(task.id)
        ):
            return True
    return False


def snapshot_for_trade_date(db: Session, portfolio_id: int, trade_date: date) -> str | None:
    cutoff = datetime.combine(trade_date, time(20, 30), SHANGHAI).astimezone(timezone.utc).replace(tzinfo=None)
    return db.scalar(select(StrategyExecutionSnapshot.id).where(
        StrategyExecutionSnapshot.portfolio_id == portfolio_id,
        StrategyExecutionSnapshot.snapshot_type == "save_and_apply",
        StrategyExecutionSnapshot.effective_from <= cutoff,
    ).order_by(StrategyExecutionSnapshot.effective_from.desc(), StrategyExecutionSnapshot.snapshot_no.desc()).limit(1))


def original_plan_retry_allowed(db: Session, task: AsyncTaskRecord | None, portfolio_id: int, trade_date: date,
                                snapshot_id: str | None = None) -> bool:
    if not task or task.task_type != TASK_TYPE or task.status not in {"failed", "partial", "cancelled"}:
        return False
    if task.id in _ACTIVE_TASK_IDS or async_tasks.is_worker_running(task.id):
        return False
    payload = json.loads(task.payload_json or "{}")
    snapshot_id = snapshot_id or payload.get("strategy_snapshot_id")
    if not snapshot_id or payload.get("portfolio_id") != portfolio_id or payload.get("trade_date") != trade_date.isoformat():
        return False
    runs = list(db.scalars(select(DecisionRun).where(DecisionRun.portfolio_id == portfolio_id,
        DecisionRun.trade_date == trade_date, DecisionRun.run_type == "auto_simulation")))
    from app.services.decision_engine import can_retry_blocked_decision
    ready = [run for run in runs if run.blocking_status == "READY"]
    if len(ready) > 1:
        return False
    for run in runs:
        if run.blocking_status == "READY":
            if run.strategy_snapshot_id != snapshot_id or run.status != "SUCCEEDED":
                return False
        elif not can_retry_blocked_decision(db, run.id, portfolio_id=portfolio_id,
            strategy_snapshot_id=snapshot_id, trade_date=trade_date):
            return False
    return bool(runs)


def latest_original_plan_retry(db: Session, portfolio_id: int, trade_date: date) -> bool:
    key = f"auto_simulation|{portfolio_id}|{trade_date.isoformat()}"
    previous = db.scalar(select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == TASK_TYPE,
        or_(AsyncTaskRecord.idempotency_key == key, AsyncTaskRecord.idempotency_key.startswith(key + "|retry:")),
    ).order_by(AsyncTaskRecord.created_at.desc()).limit(1))
    return original_plan_retry_allowed(db, previous, portfolio_id, trade_date)


def create_auto_simulation_task(db: Session, portfolio_id: int, trade_date: date, *, actor: str) -> dict:
    key = f"auto_simulation|{portfolio_id}|{trade_date.isoformat()}"
    with _DISPATCH_LOCK:
        existing = db.scalar(select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_type == TASK_TYPE,
            or_(AsyncTaskRecord.idempotency_key == key, AsyncTaskRecord.idempotency_key.startswith(key + "|retry:")),
            AsyncTaskRecord.status.in_(("queued", "running", "done")),
        ).order_by(AsyncTaskRecord.created_at.desc()).limit(1))
        created = existing is None
        if existing is None:
            previous = db.scalar(select(AsyncTaskRecord).where(
                AsyncTaskRecord.task_type == TASK_TYPE,
                or_(AsyncTaskRecord.idempotency_key == key, AsyncTaskRecord.idempotency_key.startswith(key + "|retry:")),
            ).order_by(AsyncTaskRecord.created_at.desc()).limit(1))
            old_payload = json.loads(previous.payload_json or "{}") if previous else {}
            existing = AsyncTaskRecord(id=uuid4().hex, task_type=TASK_TYPE, status="queued", stage="queued",
                message="等待开始自动推演", idempotency_key=key if previous is None else key + "|retry:" + uuid4().hex[:8],
                correlation_id=uuid4().hex[:8],
                payload_json=json.dumps({"portfolio_id": portfolio_id, "trade_date": trade_date.isoformat(),
                    "strategy_snapshot_id": old_payload.get("strategy_snapshot_id") or snapshot_for_trade_date(db, portfolio_id, trade_date),
                    "retry_of": previous.id if previous else None,
                    "operator_id": actor, "resume_mode": False}, ensure_ascii=False))
            db.add(existing)
            db.commit()
            db.refresh(existing)
        task_id, status = existing.id, existing.status
        if status == "queued":
            dispatch_execution_tasks([task_id])
        return {"portfolio_id": portfolio_id, "trade_date": trade_date.isoformat(),
                "task_id": task_id, "status": status, "created": created}


def dispatch_execution_tasks(task_ids: list[str]) -> None:
    """Reattach an orphan queued task once; never duplicate a live worker."""
    with _DISPATCH_LOCK:
        ids = [tid for tid in task_ids if tid not in _ACTIVE_TASK_IDS and not async_tasks.is_worker_running(tid)]
        if not ids:
            return
        _ACTIVE_TASK_IDS.update(ids)
        try:
            async_tasks._start_worker(ids[0], lambda _tid: _execute_tasks(ids))
        except Exception:
            _ACTIVE_TASK_IDS.difference_update(ids)
            raise


def _execute_tasks(task_ids: list[str]) -> None:
    stop_reason = None
    try:
        for task_id in task_ids:
            if stop_reason:
                db = get_session_local()()
                try:
                    task = db.get(AsyncTaskRecord, task_id)
                    if task and task.status == "queued":
                        async_tasks._append_error(task, {"code": "PREVIOUS_DAY_INCOMPLETE", "title_zh": "前一交易日尚未完成",
                            "detail_zh": stop_reason, "retryable": True})
                        async_tasks._set_task(db, task_id, status="failed", stage="waiting_previous_day",
                            message="前一交易日没有完成，后续日期已停止", finished_at=async_tasks._now())
                finally:
                    db.close()
                continue
            if not _execute_task(task_id):
                stop_reason = "请先处理前一交易日的失败或待处理订单，再重新恢复。"
    finally:
        with _DISPATCH_LOCK:
            _ACTIVE_TASK_IDS.difference_update(task_ids)


def _execute_task(task_id: str) -> bool:
    from app.services.auto_trade_dual_run import run_dual_trade
    db = get_session_local()()
    portfolio_id = None
    owns_running_state = False
    actor = "system:portfolio_execution"
    result = None
    try:
        claim = db.execute(update(AsyncTaskRecord).where(AsyncTaskRecord.id == task_id,
            AsyncTaskRecord.status == "queued", or_(AsyncTaskRecord.is_terminal_locked == 0, AsyncTaskRecord.is_terminal_locked.is_(None)))
            .values(status="running", stage="checking", message="正在检查执行条件", started_at=async_tasks._now(),
                heartbeat_at=async_tasks._now(), updated_at=async_tasks._now()))
        db.commit()
        if claim.rowcount != 1:
            return False
        task = db.get(AsyncTaskRecord, task_id)
        payload = json.loads(task.payload_json or "{}")
        portfolio_id = int(payload["portfolio_id"])
        trade_date = date.fromisoformat(payload["trade_date"])
        actor = payload.get("operator_id") or actor
        if task.cancel_requested or async_tasks.is_worker_stop_requested(task_id):
            async_tasks._set_task(db, task_id, status="cancelled", stage="cancelled", message="已停止执行", finished_at=async_tasks._now())
            return False
        portfolio = db.get(Portfolio, portfolio_id, populate_existing=True)
        if portfolio is None:
            raise ValueError("组合不存在")
        from app.services.portfolio_reconciliation_activation import assert_execution_within_active_coverage
        assert_execution_within_active_coverage(db, portfolio_id, trade_date)
        state = _get_status(db, portfolio)
        if state != "READY" or resolve_composite_status(portfolio).composite_status.value != "READY":
            raise ValueError("组合尚未就绪，请先处理检查与对账中的未完成事项")
        if payload.get("resume_mode"):
            from app.services.portfolio_resume_service import _is_trade_date, review_resume_execution_date
            review = review_resume_execution_date(db, portfolio_id, trade_date,
                strategy_snapshot_id=payload.get("strategy_snapshot_id"), current_read=True)
            if not review["enabled"]:
                raise ValueError(review["disabled_code"] + ": " + review["reason"])
            if not _is_trade_date(db, trade_date):
                raise ValueError("缺少该日期的有效交易日历，请先更新交易日历，再恢复执行。")
        snapshot_id = payload.get("strategy_snapshot_id")
        if not snapshot_id:
            # Old queued tasks did not save a snapshot. Resolve only the
            # original day's effective strategy, and retain it on future retries.
            snapshot_id = snapshot_for_trade_date(db, portfolio_id, trade_date)
            payload["strategy_snapshot_id"] = snapshot_id
            task.payload_json = json.dumps(payload, ensure_ascii=False)
            db.commit()
        if not snapshot_id:
            raise ValueError("缺少该交易日已应用的策略记录，请先检查策略，不能用今天的策略补算过去")
        if not payload.get("resume_mode"):
            from app.services import decision_schedule_gate as schedule
            now = schedule.as_shanghai(schedule.shanghai_now())
            gate = schedule.ensure_auto_simulation_schedule(now, portfolio_id, db=db)
            original_retry = original_plan_retry_allowed(db, db.get(AsyncTaskRecord, payload.get("retry_of"))
                if payload.get("retry_of") else None, portfolio_id, trade_date, snapshot_id)
            if trade_date != now.date() or (not gate.allowed and not (
                gate.error_code == "DUAL_EXECUTION_RISK_PROHIBITED" and original_retry
            )):
                raise ValueError(gate.message or "当前北京时间日期与执行日期不一致")
        transition_portfolio_state(db, portfolio_id, "RUNNING_AUTO_SIMULATION", operator_id=actor,
            correlation_id=task.correlation_id, reason="portfolio worker started")
        owns_running_state = True
        async_tasks._set_task(db, task_id, stage="executing", message=f"正在处理 {trade_date.isoformat()}",
            total=1, current_item=trade_date.isoformat(), heartbeat_at=async_tasks._now())
        def cancelled() -> bool:
            control_db = get_session_local()()
            try:
                current = control_db.get(AsyncTaskRecord, task_id)
                return bool(not current or current.cancel_requested or current.status == "cancelled"
                    or async_tasks.is_worker_stop_requested(task_id))
            finally:
                control_db.close()
        db.info["portfolio_execution_cancelled"] = cancelled
        if payload.get("resume_mode"):
            # The review admits an older signal only when its immutable plan
            # was originally due today. Use that verified execution session as
            # the bar boundary, rather than freezing it at yesterday's signal.
            # Dates after the actual current session remain unavailable.
            db.info["portfolio_resume_processing_day"] = (
                date.fromisoformat(review["source_refs"][0]["execution_date"])
                if review["mode"] == "CURRENT_ORIGINAL_PLAN" else trade_date
            )
        # The strict shared path retains account/switch/model/data/entry gates,
        # order idempotency, fees, market rules and cancellation checks.
        result = run_dual_trade(db, portfolio_id=portfolio_id, trade_date=trade_date,
            strategy_snapshot_id=snapshot_id, dry_run=False, save_diff=False,
            for_schedule=False, require_readiness=True)
        inner = result.get("execution_result") or {}
        if cancelled() or inner.get("skipped_due_to_cancel"):
            async_tasks._set_task(db, task_id, status="cancelled", stage="cancelled",
                message="已停止执行，已完成的成交保留", finished_at=async_tasks._now(),
                result_json=json.dumps(result, ensure_ascii=False, default=str))
            return False
        run = db.get(DecisionRun, inner.get("decision_run_id")) if inner.get("decision_run_id") else None
        if inner.get("blocked") or inner.get("errors") or not run or (payload.get("resume_mode") and run.trade_date != trade_date) or run.status != "SUCCEEDED" or run.blocking_status != "READY":
            reasons = json.loads(run.blocking_reasons_json or "[]") if run else []
            messages = [item.get("message") for item in reasons if isinstance(item, dict) and item.get("message")]
            messages.extend(str(value) for value in inner.get("errors", []))
            if not messages:
                messages.extend(str(value) for value in result.get("errors", []))
            if run and run.status == "SUCCEEDED" and run.blocking_status != "READY":
                # A complete blocked calculation is useful audit evidence.
                # Keep it; a data-repair retry must create its own attempt.
                db.commit()
            raise ValueError("；".join(messages) or "本次推演没有完成，请查看任务详情中的数据、策略或执行原因")
        run.task_id = task_id
        run.correlation_id = task.correlation_id
        waiting = bool(inner.get("pending_orders"))
        daily_processed = bool(payload.get("resume_mode"))
        if daily_processed:
            from app.models.sim_account import SimOrder
            for item in inner.get("pending_orders", []):
                order = db.get(SimOrder, item["order_id"]) if item.get("order_id") else None
                if not item.get("order_plan_id") or (item.get("reason") not in {
                    "EXECUTION_BAR_MISSING", "EXECUTION_TIME_NOT_REACHED", "PARTIAL_FILL_PENDING", "PARTIAL_FILL", "PARTIAL_FILL_RETRY"
                } and (order is None or order.status != "partial")):
                    raise ValueError("当天仍有无法自动继续的订单，后续日期已停止，请查看原订单原因后再试。")
            inner["pending_order_count"] = len(inner.get("pending_orders", []))
            inner["pending_order_plan_ids"] = sorted({str(item["order_plan_id"])
                for item in inner.get("pending_orders", []) if item.get("order_plan_id")})
        db.refresh(portfolio)
        if (daily_processed or not waiting) and (portfolio.last_decision_trade_date is None or portfolio.last_decision_trade_date < trade_date):
            portfolio.last_decision_trade_date = trade_date
        async_tasks._set_task(db, task_id, status="partial" if waiting and not daily_processed else "done",
            stage="waiting_market_data" if waiting and not daily_processed else "done", percent=100, processed=1,
            ok_count=0 if waiting and not daily_processed else 1, result_json=json.dumps(result, ensure_ascii=False, default=str),
            message=("当天处理完成，仍有订单待成交" if waiting and daily_processed
                else "推演已完成，仍有订单等待行情" if waiting else "自动推演已完成"), finished_at=async_tasks._now())
        # Source receipts were committed atomically with account/task writes.
        # The reconciliation scanner consumes them without dispatching trades.
        return daily_processed or not waiting
    except Exception as exc:
        db.rollback()
        task = db.get(AsyncTaskRecord, task_id, populate_existing=True)
        if task:
            if task.cancel_requested or task.status == "cancelled":
                async_tasks._set_task(db, task_id, status="cancelled", stage="cancelled",
                    message="已停止执行，已完成的成交保留", finished_at=async_tasks._now())
                return False
            async_tasks._append_error(task, {"code": "PORTFOLIO_EXECUTION_FAILED", "title_zh": "自动推演没有完成",
                "detail_zh": str(exc), "retryable": True})
            async_tasks._set_task(db, task_id, status="failed", stage="failed", failed_count=1,
                message="自动推演没有完成，请查看原因后重试", finished_at=async_tasks._now(),
                result_json=json.dumps(result, ensure_ascii=False, default=str) if result is not None else task.result_json)
        return False
    finally:
        db.info.pop("portfolio_execution_cancelled", None)
        db.info.pop("portfolio_resume_processing_day", None)
        try:
            if owns_running_state:
                portfolio = db.get(Portfolio, portfolio_id, populate_existing=True)
                if portfolio and _get_status(db, portfolio) == "RUNNING_AUTO_SIMULATION":
                    transition_portfolio_state(db, portfolio_id, "READY", operator_id=actor, reason="portfolio worker ended")
                    db.commit()
        finally:
            db.close()
