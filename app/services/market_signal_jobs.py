"""Nonblocking UI operations on the existing persistent task/worker runtime."""
import json

from fastapi import HTTPException
from sqlalchemy import select

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.services.async_tasks import _set_task, _start_worker, create_async_task, get_async_task

TASK_TYPE = "market_signals"


def get_job(task_id):
    value = get_async_task(task_id)
    if value is None or value.task_type != TASK_TYPE:
        raise HTTPException(404, "异动任务不存在")
    return value.model_dump()


def create_job(payload):
    factory = get_session_local()
    with factory() as db:
        existing = db.scalar(select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_type == TASK_TYPE, AsyncTaskRecord.status.in_(("queued", "running"))
        ).order_by(AsyncTaskRecord.created_at.desc()).limit(1))
        if existing:
            if json.loads(existing.payload_json) == payload.model_dump():
                return get_job(existing.id)
            raise HTTPException(409, "已有异动后台任务正在运行，请等待完成后重试")
    task = create_async_task(TASK_TYPE, payload.model_dump(), force_new=True)
    _start_worker(task.id, _worker)
    return task.model_dump()


def _worker(task_id):
    factory = get_session_local()
    with factory() as db:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        payload = json.loads(task.payload_json)
        _set_task(db, task_id, status="running", stage=payload["kind"], percent=5, message="正在执行异动提醒任务")
        from app.schemas.market_signal import SignalAnalyzeRequest
        if payload["kind"] == "monitor":
            from app.services.market_signal_operations import monitor
            result = monitor(db, analyze=payload["analyze"], progress=lambda stage, percent, message:
                _set_task(db, task_id, stage=stage, percent=percent, message=message))
        elif payload["kind"] == "analyze":
            from app.services.market_signal_analysis import analyze
            result = analyze(db, SignalAnalyzeRequest(evidence_ids=payload["evidence_ids"],
                rule_ids=payload["rule_ids"], retry_failed=payload["retry_failed"]))
        elif payload["kind"] == "import_legacy":
            from app.services.market_signal_operations import import_legacy
            result = import_legacy(db)
        elif payload["kind"] == "enrich":
            from app.services.market_signal_operations import enrich_evidence
            result = enrich_evidence(db, payload["evidence_id"])
        elif payload["kind"] == "sync_data":
            from app.services.market_signal_operations import sync_required_data
            result = sync_required_data(db, payload["rule_ids"])
        else:
            raise ValueError("该任务类型尚不可用")
        status = "partial" if result.get("partial") else "done"
        _set_task(db, task_id, status=status, stage=status, percent=100,
                  result_json=json.dumps(result, ensure_ascii=False, default=str),
                  message="部分数据暂不可用，请查看阻塞原因" if status == "partial" else "异动任务完成")
