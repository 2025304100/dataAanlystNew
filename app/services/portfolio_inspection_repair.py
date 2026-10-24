"""Confirmed, scoped data repairs on the existing async task infrastructure.

Only market data, explicit rule-score outputs and proven data dimensions are written. No trading,
trade-plan, scan, position, reconciliation cursor or cash service is called.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio
from app.models.scoring_config import ScoringConfig
from app.models.symbol import Symbol
from app.schemas.portfolio_inspection import InspectionRepairSubmit
from app.services.async_tasks import _set_task, _start_worker, is_worker_running, is_worker_stop_requested
from app.services.data_governance_audit import write_audit_event
from app.services.market_data_errors import MarketHistorySourceError
from app.services.portfolio_inspection_diagnostics import (
    _hash, _object, _utc, build_portfolio_inspection, resolve_inspection_calendar,
)

logger = logging.getLogger(__name__)
TASK_TYPE = "portfolio_data_repair"
TERMINAL = {"done", "partial", "failed", "cancelled"}


class InspectionRepairError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def _audit(db: Session, task_id: str, portfolio_id: int, kind: str, *, after=None, symbol_id=None, actor=None):
    # The existing audit whitelist supports this compatibility envelope. The
    # requested business event stays explicit; never label a repair as a trade.
    task = db.get(AsyncTaskRecord, task_id)
    payload = _object(task.payload_json) if task else {}
    operations = payload.get("operations", [])
    if isinstance(after, dict) and after.get("operation_id"):
        operations = [op for op in operations if op.get("operation_id") == after["operation_id"]]
    if symbol_id is not None:
        operations = [op for op in operations if op.get("symbol_id") == symbol_id]
    issue_ids = sorted({eid for op in operations for eid in op.get("resolves_evidence_ids", [])})
    saved_objects = {row["operation_id"]: row for row in _object(task.result_json).get("objects", [])} if task else {}
    if isinstance(after, dict):
        saved_objects.update({row["operation_id"]: row for row in after.get("objects", [])})
    object_rows = []
    for operation in operations:
        saved = saved_objects.get(operation["operation_id"], {})
        actual_result = after.get("status") if isinstance(after, dict) and after.get("operation_id") else saved.get("status", "pending")
        object_rows.append({"object_id": operation["operation_id"], "symbol_id": operation.get("symbol_id"),
            "symbol": operation.get("symbol"), "trade_date": operation.get("trade_date"),
            "trade_date_from": operation.get("start_date"), "trade_date_to": operation.get("end_date"),
            "result": actual_result, "reinspection_status": saved.get("reinspection_status"),
            "remaining_evidence_ids": saved.get("remaining_evidence_ids", [])})
    result = write_audit_event(db, "UNKNOWN_AUDIT_ACTION", portfolio_id=portfolio_id,
        symbol_id=symbol_id, business_key=task_id, occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
        correlation_id=task.correlation_id if task else task_id[:8],
        operator_id=actor or payload.get("actor") or "unrecorded_actor",
        after=after, attributes={"original_action": kind, "event_family": "portfolio_inspection_repair",
            "issue_ids": issue_ids, "snapshot_id": payload.get("snapshot_id"), "plan_id": payload.get("plan_id"),
            "executed_by": "request_handler" if kind in {"REPAIR_TASK_REQUESTED", "REPAIR_TASK_CANCEL_REQUESTED"} else "portfolio-data-repair-worker",
            "objects": object_rows, "task_id": task_id,
            "report_refs": [f"async_task:{task_id}:reinspection"] if kind == "REPAIR_TASK_RESULT" else [],
            "task_ref": f"async_task:{task_id}"})
    return f"data_governance_audit_event:{result.event_id}"


def _task_payload(task: AsyncTaskRecord, portfolio_id: int) -> dict:
    try:
        payload = _object(task.payload_json)
    except (TypeError, ValueError):
        raise InspectionRepairError("REPAIR_TASK_CORRUPT", "修复任务范围记录无法解析") from None
    if task.task_type != TASK_TYPE or payload.get("portfolio_id") != portfolio_id:
        raise LookupError("Repair task not found")
    return payload


def get_inspection_repair_task(db: Session, portfolio_id: int, task_id: str) -> dict:
    with db.no_autoflush:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            raise LookupError("Repair task not found")
        payload = _task_payload(task, portfolio_id)
        result = _object(task.result_json)
        objects = result.get("objects") or [{**operation, "status": "pending"} for operation in payload["operations"]]
        retryable_ids = [op["operation_id"] for op in objects if op.get("status") in {"failed", "dependency_failed", "cancelled"}
            or (task.status in {"failed", "cancelled"} and op.get("status") == "pending")] if task.status in TERMINAL else []
        try:
            errors = json.loads(task.errors_json or "[]")
        except (ValueError, TypeError):
            errors = []
        codes = {str(row["error_code"]) for row in objects if row.get("error_code")}
        if isinstance(errors, list):
            codes.update(str(err["code"]) for err in errors if isinstance(err, dict) and err.get("code"))
        error_codes = sorted(codes)
        worker_exiting = task.status in TERMINAL and is_worker_running(task.id)
        if worker_exiting:
            retryable_ids = []
        return {"task_id": task.id, "portfolio_id": portfolio_id, "status": task.status, "stage": task.stage,
            "percent": task.percent or 0, "total": task.total or len(objects), "processed": task.processed or 0,
            "ok_count": task.ok_count or 0, "failed_count": task.failed_count or 0,
            "message": task.message or "", "objects": objects,
            "retryable_operation_ids": retryable_ids,
            "created_by": payload.get("actor"), "created_at": _utc(task.created_at) if task.created_at else None,
            "updated_at": _utc(task.updated_at) if task.updated_at else None,
            "started_at": _utc(task.started_at) if task.started_at else None,
            "finished_at": _utc(task.finished_at) if task.finished_at else None,
            "cancel_requested": bool(task.cancel_requested),
            "can_cancel": task.status not in TERMINAL and not task.cancel_requested,
            "recovery": {"status": "WAITING_WORKER" if worker_exiting else "RETRY_AVAILABLE" if retryable_ids else "STOP_REQUESTED" if task.cancel_requested and task.status not in TERMINAL else "NONE",
                "reason": "原执行器尚未退出，等待当前对象检查点保存后才能重试" if worker_exiting else "已保存的成功产物保留，可仅重试未完成对象" if retryable_ids else
                          "当前对象完成后停止，尚未开始的对象不会执行" if task.cancel_requested and task.status not in TERMINAL else
                          "读取任务状态不启动或重新执行任何对象",
                "error_codes": error_codes},
            "parent_task_id": payload.get("parent_task_id"), "reinspection": result.get("reinspection"),
            "dimension_recovery": result.get("dimension_recovery", {}),
            "audit_refs": result.get("audit_refs", []), "correlation_id": task.correlation_id,
            "issue_ids": sorted({eid for op in payload["operations"] for eid in op.get("resolves_evidence_ids", [])}),
            "report_refs": [f"async_task:{task_id}:reinspection"] if result.get("reinspection") else [],
            "replayed": False}


def list_inspection_repair_tasks(db: Session, portfolio_id: int, *, limit: int = 20, cursor: str | None = None) -> dict:
    """Read persisted, portfolio-scoped history without running housekeeping.

    The legacy task table has no portfolio column. Parse the authoritative
    payload rather than matching a portfolio id as a substring. The stable
    keyset cursor is itself checked for ownership before any rows are returned.
    """
    if not 1 <= limit <= 100:
        raise InspectionRepairError("REPAIR_PAGE_SIZE_INVALID", "每页任务数量须为 1 至 100", status_code=422)
    with db.no_autoflush:
        statement = select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == TASK_TYPE)
        if cursor:
            anchor = db.get(AsyncTaskRecord, cursor)
            if anchor is None:
                raise LookupError("Repair task not found")
            _task_payload(anchor, portfolio_id)
            statement = statement.where(or_(AsyncTaskRecord.created_at < anchor.created_at,
                and_(AsyncTaskRecord.created_at == anchor.created_at, AsyncTaskRecord.id < anchor.id)))
        statement = statement.order_by(AsyncTaskRecord.created_at.desc(), AsyncTaskRecord.id.desc()).execution_options(yield_per=100)
        ids = []
        for row in db.scalars(statement):
            try:
                _task_payload(row, portfolio_id)
            except (LookupError, InspectionRepairError):
                continue
            ids.append(row.id)
            if len(ids) > limit:
                break
        return {"portfolio_id": portfolio_id,
            "items": [get_inspection_repair_task(db, portfolio_id, task_id) for task_id in ids[:limit]],
            "next_cursor": ids[limit - 1] if len(ids) > limit else None}


def cancel_inspection_repair(db: Session, portfolio_id: int, task_id: str, *, actor: str) -> dict:
    """Stop only work that has not started; never delete committed artifacts.

    Queued cancellation and the worker's claim serialize on the task row.
    Running cancellation is cooperative: the active object can finish, then
    the worker marks remaining objects cancelled and performs reinspection.
    Repeating this action for the same task is naturally idempotent.
    """
    task = db.get(AsyncTaskRecord, task_id, with_for_update=True, populate_existing=True)
    if task is None:
        raise LookupError("Repair task not found")
    payload = _task_payload(task, portfolio_id)
    if task.status in TERMINAL or task.cancel_requested:
        return {**get_inspection_repair_task(db, portfolio_id, task_id), "replayed": True}
    task.cancel_requested = 1
    task.cancelled_timeout_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=60)
    if task.status == "queued":
        result = _object(task.result_json)
        result.setdefault("objects", [{**operation, "status": "pending"} for operation in payload["operations"]])
        for operation in result.get("objects", []):
            if operation.get("status") == "pending":
                operation.update(status="cancelled", reason="本人已取消尚未执行的修复对象；已保存产物保留")
        task.result_json = json.dumps(result, ensure_ascii=False)
        task.status, task.stage, task.is_terminal_locked = "cancelled", "cancelled", 1
        task.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        task.message = "修复已取消，尚未执行的对象未启动；已保存产物保留"
    try:
        audit_ref = _audit(db, task_id, portfolio_id, "REPAIR_TASK_CANCEL_REQUESTED", actor=actor,
            after={"status": task.status, "cancel_requested": True, "preserve_successful_artifacts": True})
        result = _object(task.result_json)
        result.setdefault("audit_refs", []).append(audit_ref)
        task.result_json = json.dumps(result, ensure_ascii=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_inspection_repair_task(db, portfolio_id, task_id)


def _persist_and_start(db: Session, portfolio_id: int, payload: dict, key: str) -> dict:
    task_id = _hash({"portfolio_id": portfolio_id, "key": key})[:32]
    fingerprint = _hash(payload)
    existing = db.get(AsyncTaskRecord, task_id)
    if existing:
        _task_payload(existing, portfolio_id)
        if existing.idempotency_key != "request:" + fingerprint:
            raise InspectionRepairError("REPAIR_REQUEST_CONFLICT", "相同请求键已用于不同修复范围")
        return {**get_inspection_repair_task(db, portfolio_id, task_id), "replayed": True}
    task = AsyncTaskRecord(id=task_id, task_type=TASK_TYPE, status="queued", stage="queued",
        percent=0, total=len(payload["operations"]), processed=0, ok_count=0, failed_count=0,
        message="已确认修复范围，等待数据修复作业", payload_json=json.dumps(payload, ensure_ascii=False),
        idempotency_key="request:" + fingerprint, correlation_id=task_id[:8], is_terminal_locked=0,
        result_json=json.dumps({"objects": [{**op, "status": "pending"} for op in payload["operations"]]}, ensure_ascii=False))
    db.add(task)
    try:
        db.flush()
        audit_ref = _audit(db, task_id, portfolio_id, "REPAIR_TASK_REQUESTED", after={
            "plan_id": payload.get("plan_id"), "operations": payload["operations"], "parent_task_id": payload.get("parent_task_id")})
        result = _object(task.result_json)
        result["audit_refs"] = [audit_ref]
        task.result_json = json.dumps(result, ensure_ascii=False)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(AsyncTaskRecord, task_id)
        if existing and existing.idempotency_key == "request:" + fingerprint:
            return {**get_inspection_repair_task(db, portfolio_id, task_id), "replayed": True}
        raise InspectionRepairError("REPAIR_REQUEST_CONFLICT", "修复请求发生并发冲突，请重新检查") from None
    except Exception:
        db.rollback()
        raise
    try:
        _start_worker(task_id, _run_inspection_repair)
    except Exception:
        logger.exception("Could not start portfolio repair worker task=%s", task_id)
        _set_task(db, task_id, status="failed", stage="failed", message="修复作业未能启动，请重试失败项",
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
        raise InspectionRepairError("REPAIR_DISPATCH_FAILED", "修复作业未能启动，已保存失败记录", status_code=503) from None
    return get_inspection_repair_task(db, portfolio_id, task_id)


def submit_inspection_repair(db: Session, portfolio_id: int, request: InspectionRepairSubmit, *, actor: str = "local_user") -> dict:
    # Repeated clicks must return the original task even after its outputs have
    # changed the inspection fingerprint. Validate the original request first.
    request_fingerprint = _hash({"request": request.model_dump(mode="json"), "actor": actor})
    task_id = _hash({"portfolio_id": portfolio_id, "key": request.idempotency_key})[:32]
    with db.no_autoflush:
        existing = db.get(AsyncTaskRecord, task_id)
        if existing:
            payload = _task_payload(existing, portfolio_id)
            if payload.get("request_fingerprint") != request_fingerprint:
                raise InspectionRepairError("REPAIR_REQUEST_CONFLICT", "相同请求键已用于不同修复范围")
            return {**get_inspection_repair_task(db, portfolio_id, task_id), "replayed": True}
        now = datetime.now(timezone.utc)
        generated = _utc(request.generated_at)
        if generated > now or now - generated > timedelta(minutes=15):
            raise InspectionRepairError("REPAIR_PLAN_EXPIRED", "修复预览已过期，请重新检查数据后确认")
        inspection = build_portfolio_inspection(db, portfolio_id, current_read=True)
        plan = inspection["repair_plan"]
        if plan["plan_id"] != request.plan_id or inspection["snapshot_id"] != request.snapshot_id:
            raise InspectionRepairError("INSPECTION_SNAPSHOT_CHANGED", "数据或配置已变化，请查看新的检查结果后确认")
        operation_map = {op["operation_id"]: op for op in plan["operations"]}
        selected = set(request.operation_ids)
        if len(selected) != len(request.operation_ids) or not selected.issubset(operation_map):
            raise InspectionRepairError("REPAIR_OPERATION_INVALID", "修复范围包含重复或未预览的对象", status_code=422)
        operations = [operation_map[oid] for oid in request.operation_ids]
        if any(not op["enabled"] for op in operations):
            raise InspectionRepairError("REPAIR_OPERATION_DISABLED", "所选对象包含当前没有执行能力的动作", status_code=422)
        if any(not set(op["depends_on"]).issubset(selected) for op in operations):
            raise InspectionRepairError("REPAIR_DEPENDENCY_REQUIRED", "请先补齐或同时选择上游日K输入", status_code=422)
        # Use server order (upstream sync before calculation), not client order.
        operations = [op for op in plan["operations"] if op["operation_id"] in selected]
        payload = {"portfolio_id": portfolio_id, "plan_id": request.plan_id, "snapshot_id": request.snapshot_id,
            "actor": actor,
            "operations": operations, "request_fingerprint": request_fingerprint,
            "binding": inspection["binding"], "policy_version": inspection["policy_version"]}
    return _persist_and_start(db, portfolio_id, payload, request.idempotency_key)


def retry_inspection_repair(db: Session, portfolio_id: int, task_id: str, *, idempotency_key: str, actor: str = "local_user") -> dict:
    with db.no_autoflush:
        prior = db.get(AsyncTaskRecord, task_id)
        if prior is None:
            raise LookupError("Repair task not found")
        prior_payload = _task_payload(prior, portfolio_id)
        state = get_inspection_repair_task(db, portfolio_id, task_id)
        if prior.status not in TERMINAL:
            raise InspectionRepairError("REPAIR_TASK_RUNNING", "原任务仍在执行，请等待逐对象结果")
        if is_worker_running(task_id):
            raise InspectionRepairError("REPAIR_WORKER_STOPPING", "原执行器尚未退出，请等待当前对象检查点保存后重试")
        retry_ids = set(state["retryable_operation_ids"])
        # A dispatch/crash failure can precede the first object update. Those
        # untouched objects are eligible; completed objects are always retained.
        if prior.status in {"failed", "cancelled"}:
            retry_ids.update(op["operation_id"] for op in state["objects"] if op.get("status") == "pending")
        if not retry_ids:
            raise InspectionRepairError("REPAIR_NO_FAILED_OBJECTS", "没有可重试的失败对象", status_code=422)
        operations = [dict(op) for op in prior_payload["operations"] if op["operation_id"] in retry_ids]
        succeeded = {op["operation_id"] for op in state["objects"] if op.get("status") == "succeeded"}
        for op in operations:
            op["depends_on"] = [dep for dep in op["depends_on"] if dep not in succeeded]
        payload = {**prior_payload, "operations": operations, "parent_task_id": task_id,
            "actor": actor, "request_fingerprint": _hash({"parent_task_id": task_id, "idempotency_key": idempotency_key, "actor": actor})}
    return _persist_and_start(db, portfolio_id, payload, idempotency_key)


def _execute_operation(db: Session, operation: dict) -> dict:
    symbol = db.get(Symbol, operation["symbol_id"])
    if symbol is None:
        raise InspectionRepairError("REPAIR_SYMBOL_MISSING", "标的基本资料已不存在")
    day = date.fromisoformat(operation["trade_date"])
    if operation["kind"] == "sync_daily_bars":
        from app.services.market_data import sync_symbol_daily_bars
        start, end = date.fromisoformat(operation["start_date"]), date.fromisoformat(operation["end_date"])
        calendar = resolve_inspection_calendar(db, market=symbol.market)
        if calendar["status"] != "PASSED":
            raise InspectionRepairError("REPAIR_CALENDAR_CHANGED", "交易日历无法核验，请重新检查后生成修复计划")
        expected_days = {d for d in calendar["trading_dates"] if start <= d <= end}
        if not expected_days or day not in expected_days:
            raise InspectionRepairError("REPAIR_CALENDAR_CHANGED", "所确认日期不在当前可核验交易日历范围")
        outcome = sync_symbol_daily_bars(db=db, symbol=symbol,
            start_date=start, end_date=end, adjust="qfq")
        db.flush()
        produced = list(db.execute(select(DailyBar).where(DailyBar.symbol_id == symbol.id,
            DailyBar.trade_date.in_(expected_days))).scalars())
        if outcome.get("status") != "ok" or {row.trade_date for row in produced} != expected_days:
            raise InspectionRepairError("REPAIR_BAR_NOT_PRODUCED", "同步完成但所确认范围内仍有应到交易日行情未生成")
        required_fields = [eid.split(":bar_input:", 1)[1] for eid in operation.get("resolves_evidence_ids", []) if ":bar_input:" in eid]
        latest = next(row for row in produced if row.trade_date == day)
        if any(getattr(latest, field, None) is None for field in required_fields):
            raise InspectionRepairError("REPAIR_BAR_INPUT_NOT_PRODUCED", "同步后所确认的原始行情字段仍缺失，不能标记修复成功")
        return {"source_record_ref": f"daily_bar:{latest.id}", "actual_trade_date": day.isoformat(),
            "verified_trade_dates": sorted(d.isoformat() for d in expected_days), "verified_fields": required_fields,
            **({'history_evidence': outcome['history_evidence']} if outcome.get('history_evidence') else {})}
    if operation["kind"] == "recalculate_rule_score":
        from app.services.scoring_config_engine import calculate_symbol_score_with_config
        config = db.get(ScoringConfig, operation["config_id"], with_for_update=True, populate_existing=True)
        if (config is None or config.version != operation["config_version"] or config.asset_type != symbol.asset_type
                or not config.is_active):
            raise InspectionRepairError("REPAIR_CONFIG_CHANGED", "预览采用的评分配置已变化，请重新检查")
        if operation.get("config_fingerprint") and _hash(_object(config.config_json)) != operation["config_fingerprint"]:
            raise InspectionRepairError("REPAIR_CONFIG_CHANGED", "预览采用的评分配置内容已变化，请重新检查")
        bar = db.execute(select(DailyBar.id).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date == day)).scalar_one_or_none()
        if bar is None:
            raise InspectionRepairError("REPAIR_BAR_REQUIRED", "评分所需的应到交易日日K尚未生成")
        score = calculate_symbol_score_with_config(db, symbol, day, config=config)
        db.flush()
        if score.scoring_config_id != config.id or score.scoring_config_version != config.version or score.weight_mode != "manual":
            raise InspectionRepairError("REPAIR_SCORE_SCOPE_MISMATCH", "评分产物未匹配已确认的规则配置")
        return {"source_record_ref": f"score:{score.id}", "actual_trade_date": score.trade_date.isoformat(),
            "config_id": score.scoring_config_id, "config_version": score.scoring_config_version}
    raise InspectionRepairError("REPAIR_KIND_UNSUPPORTED", "该修复动作没有已接通的执行器")


def _run_inspection_repair(task_id: str) -> None:
    from app.db.session import get_session_local
    db = get_session_local()()
    try:
        claim = db.execute(update(AsyncTaskRecord).where(AsyncTaskRecord.id == task_id,
            AsyncTaskRecord.task_type == TASK_TYPE, AsyncTaskRecord.status == "queued").values(
                status="running", stage="repair", started_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.commit()
        if claim.rowcount != 1:
            return
        task = db.get(AsyncTaskRecord, task_id)
        payload = _object(task.payload_json)
        portfolio_id = payload["portfolio_id"]
        objects = [{**op, "status": "pending"} for op in payload["operations"]]
        audit_refs = _object(task.result_json).get("audit_refs", [])
        completed: dict[str, str] = {}
        cancelled = False
        for index, operation in enumerate(objects):
            db.refresh(task)
            if task.cancel_requested or task.status in TERMINAL or is_worker_stop_requested(task_id):
                cancelled = True
                for remaining in objects[index:]:
                    remaining.update(status="cancelled", reason="作业已停止，已保存的成功产物保留")
                break
            if any(completed.get(dep) != "succeeded" for dep in operation["depends_on"]):
                operation.update(status="dependency_failed", error_code="REPAIR_DEPENDENCY_FAILED", reason="上游输入修复未完成，未执行该对象")
            else:
                try:
                    # Recheck current binding before every object; a saved model
                    # switch must not let a queued rule repair claim to fix it.
                    current = build_portfolio_inspection(db, portfolio_id, current_read=True)["binding"]
                    if current != payload["binding"]:
                        raise InspectionRepairError("REPAIR_BINDING_CHANGED", "组合绑定已变化，请重新检查后生成修复计划")
                    output = _execute_operation(db, operation)
                    operation.update(status="succeeded", output=output)
                    # A concurrent cancellation can append an audit reference
                    # while an object is running. Refresh the control row and
                    # merge its refs before saving this object's checkpoint.
                    db.refresh(task)
                    audit_refs = list(dict.fromkeys(audit_refs + _object(task.result_json).get("audit_refs", [])))
                    audit_refs.append(_audit(db, task_id, portfolio_id, "REPAIR_OBJECT_RESULT", after={
                        "operation_id": operation["operation_id"], "status": "succeeded", "output": output}, symbol_id=operation["symbol_id"]))
                    # Persist result/checkpoint in the same transaction as each
                    # output, so a later failure never rolls it back.
                    task.result_json = json.dumps({"objects": objects, "audit_refs": audit_refs}, ensure_ascii=False)
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    logger.warning("Repair object failed task=%s operation=%s", task_id, operation["operation_id"], exc_info=True)
                    known = isinstance(exc, (InspectionRepairError, MarketHistorySourceError))
                    operation.update(status="failed", error_code=exc.code if known else "REPAIR_OBJECT_FAILED",
                        reason=str(exc) if known else "本步骤执行失败，原始错误已记录在本任务日志中，请核查后仅重试失败步骤。")
                    if isinstance(exc, MarketHistorySourceError):
                        operation.update(source_label=exc.source_label, next_action=exc.next_action,
                                         source_retryable=exc.retryable)
            if operation["status"] != "succeeded":
                db.refresh(task)
                audit_refs = list(dict.fromkeys(audit_refs + _object(task.result_json).get("audit_refs", [])))
                audit_refs.append(_audit(db, task_id, portfolio_id, "REPAIR_OBJECT_RESULT", after={
                    "operation_id": operation["operation_id"], "status": operation["status"],
                    "error_code": operation.get("error_code"), "reason": operation.get("reason"),
                    "source_label": operation.get("source_label"), "next_action": operation.get("next_action")}, symbol_id=operation["symbol_id"]))
            completed[operation["operation_id"]] = operation["status"]
            ok_count = sum(op["status"] == "succeeded" for op in objects)
            failed_count = sum(op["status"] in {"failed", "dependency_failed"} for op in objects)
            _set_task(db, task_id, processed=index + 1, ok_count=ok_count, failed_count=failed_count,
                percent=round((index + 1) / len(objects) * 95, 1), current_item=operation.get("symbol"),
                heartbeat_at=datetime.now(timezone.utc).replace(tzinfo=None),
                message=f"已处理 {index + 1}/{len(objects)} 项；成功 {ok_count} 项，失败 {failed_count} 项",
                result_json=json.dumps({"objects": objects, "audit_refs": audit_refs}, ensure_ascii=False))
        db.expire_all()
        try:
            # Lock the portfolio before the fixed-order current source reads.
            # Outputs were checkpointed already; only this final proof may
            # release stale data/score dimensions in the task-result transaction.
            portfolio = db.get(Portfolio, portfolio_id, with_for_update=True, populate_existing=True)
            if portfolio is None:
                raise LookupError("Portfolio no longer exists")
            reinspection = build_portfolio_inspection(db, portfolio_id, current_read=True)
        except Exception:
            db.rollback()
            reinspection = {"status": "READ_ERROR", "reason": "产物处理结束，但重检读取失败；请重新检查"}
        from app.services.portfolio_inspection_runtime import evaluate_inspection_admission_scope
        from app.services.portfolio_status import try_auto_recover_dimensions
        data_proof = evaluate_inspection_admission_scope(reinspection)
        proven = data_proof["passed"] is True and data_proof["has_admission_scope"] is True
        dimension_recovery = {
            "status": "VERIFIED" if proven else "UNKNOWN" if data_proof["passed"] is None else "BLOCKED",
            "changed_fields": [], "snapshot_id": reinspection.get("snapshot_id"),
            "aggregation_policy": data_proof.get("aggregation_policy"),
            "restricted_symbol_ids": data_proof.get("restricted_symbol_ids", []),
            "coverage": data_proof.get("coverage", {}), "reason": data_proof["reason"],
            "runtime_state_changed": False,
        }
        if proven:
            recovery = try_auto_recover_dimensions(db, portfolio_id, data_completed=True, score_completed=True)
            dimension_recovery.update(
                changed_fields=list(recovery.auto_changed_fields),
                before={key: getattr(recovery, f"{key}_before").value for key in (
                    "status_score", "status_data", "status_model", "status_reconciliation")},
                after={key: getattr(recovery, f"{key}_after").value for key in (
                    "status_score", "status_data", "status_model", "status_reconciliation")},
                manual_only_blocking_left=list(recovery.manual_only_blocking_left),
            )
        evidence_by_id = {ev["evidence_id"]: ev for row in reinspection.get("symbols", []) for ev in row.get("evidence", [])}
        evidence_by_id.update({ev["evidence_id"]: ev for ev in reinspection.get("evidence", [])})
        from app.services.portfolio_inspection_diagnostics import BAD_STATUSES
        for operation in objects:
            target_ids = operation.get("resolves_evidence_ids", [])
            relevant = [evidence_by_id[eid] for eid in target_ids if eid in evidence_by_id]
            remaining = [ev["evidence_id"] for ev in relevant if ev["status"] in BAD_STATUSES]
            unknown = reinspection.get("status") == "READ_ERROR" or len(relevant) != len(target_ids) or not target_ids
            operation["reinspection_status"] = "UNKNOWN" if unknown else ("STILL_BLOCKED" if remaining else "PASSED")
            operation["remaining_evidence_ids"] = remaining
        ok_count = sum(op["status"] == "succeeded" for op in objects)
        failed_count = sum(op["status"] in {"failed", "dependency_failed", "cancelled"} for op in objects)
        status = "cancelled" if cancelled else ("done" if not failed_count else ("partial" if ok_count else "failed"))
        audit_refs.append(_audit(db, task_id, portfolio_id, "REPAIR_TASK_RESULT", after={
            "status": status, "ok_count": ok_count, "failed_count": failed_count,
            "reinspection_snapshot_id": reinspection.get("snapshot_id"), "objects": objects,
            "dimension_recovery": dimension_recovery}))
        _set_task(db, task_id, status=status, stage="done", percent=100, processed=len(objects), ok_count=ok_count,
            failed_count=failed_count, finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            message="修复产物已保存并重新检查；剩余阻断仍需处理" if not failed_count else "部分对象未完成；成功产物已保留，可仅重试失败项",
            result_json=json.dumps({"objects": objects, "audit_refs": audit_refs, "reinspection": reinspection,
                "dimension_recovery": dimension_recovery}, ensure_ascii=False))
    finally:
        db.close()
