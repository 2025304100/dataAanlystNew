"""Exact, read-only recovery of source-recheck submissions, including commit gaps."""
import json
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.models.async_task import AsyncTaskRecord
from app.models.portfolio_inspection_workflow import ReconciliationRecheckRequest, ReconciliationRequestAnnotation
from app.services.portfolio_inspection_diagnostics import _hash


def identity(portfolio_id, request_key):
    return _hash({"portfolio_id": portfolio_id, "request_key": request_key})


def read_request(db, portfolio_id, request_key):
    row = db.get(ReconciliationRecheckRequest, identity(portfolio_id, request_key))
    if row is None or row.portfolio_id != portfolio_id or row.request_key != request_key:
        return None
    task = db.get(AsyncTaskRecord, row.task_id) if row.task_id else None
    annotations = list(db.scalars(select(ReconciliationRequestAnnotation).where(
        ReconciliationRequestAnnotation.request_id == row.id).order_by(
        ReconciliationRequestAnnotation.created_at, ReconciliationRequestAnnotation.id)))
    # Do not use get_async_task(): it performs patrol/expiry mutations.
    return {**json.loads(row.result_json), "portfolio_id": portfolio_id,
        "request_key": row.request_key, "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "task_id": row.task_id, "dispatch_status": "QUEUED" if task else "WAITING_RETRY",
        "task_status": task.status if task else None,
        "task_stage": task.stage if task else None,
        "provenance_annotations": [{"source_code": note.source_code,
            "label": "验收产生的请求关联" if note.source_code == "ACCEPTANCE_ROUTE_TEARDOWN_LEAK" else "来源补充说明",
            "note": note.note, "evidence_ref": note.evidence_ref,
            "annotated_by": note.annotated_by, "created_at": note.created_at.isoformat()} for note in annotations],
        "message": None if task else "原重检需求已保存，尚未确认任务已派发；请查看自动对账作业，不重复提交。",
        "reconciliation_passed": False, "trading_resumed": False}


def register_request(db, portfolio_id, *, request_key, trade_date, actor, register):
    rid = identity(portfolio_id, request_key)
    fingerprint = _hash({"actor": actor, "trade_date": trade_date.isoformat() if trade_date else None})
    def existing(row):
        if row.request_hash != fingerprint:
            raise ValueError("原请求标识已用于其他日期或操作人，请读取原请求，不能更改其范围")
        return row.id, False
    row = db.get(ReconciliationRecheckRequest, rid)
    if row is not None:
        return existing(row)
    row = ReconciliationRecheckRequest(id=rid, portfolio_id=portfolio_id, request_key=request_key,
        request_hash=fingerprint, trade_date=trade_date, created_by=actor)
    db.add(row)
    try:
        # Claim before consuming events/advancing evidence versions. The unique
        # key arbitrates concurrent processes; all registration effects commit together.
        db.flush()
    except IntegrityError:
        db.rollback()
        row = db.get(ReconciliationRecheckRequest, rid)
        if row is None:
            raise
        return existing(row)
    row.result_json = json.dumps(register(), ensure_ascii=False, default=str)
    return rid, True


def bind_task(db, request_id, task_id):
    if request_id is None:
        return
    row = db.get(ReconciliationRecheckRequest, request_id)
    if row is None:
        raise ValueError("原请求登记不存在，停止任务派发")
    if row.task_id is not None and row.task_id != task_id:
        raise ValueError("原请求已关联任务，不能改绑")
    row.task_id = task_id
    db.commit()
