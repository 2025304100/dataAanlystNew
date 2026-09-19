"""Asynchronous candidate tail-session proxy task."""
from __future__ import annotations

import json
import logging

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.services.async_tasks import (
    _append_error,
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    list_async_tasks,
)
from app.services.tail_proxy_data import sync_tail_proxy_snapshots


logger = logging.getLogger(__name__)
TASK_TYPE = "tail_proxy_snapshot"


def create_tail_proxy_task(*, source: str = "candidates", limit: int = 20):
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {"queued", "running"}:
        return existing[0]
    task = create_async_task(
        TASK_TYPE, {"source": source, "limit": limit}
    )
    _start_worker(task.id, _run_tail_proxy_task)
    return task


def _run_tail_proxy_task(task_id: str) -> None:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        payload = json.loads(task.payload_json or "{}")
        source = str(payload.get("source") or "candidates")
        limit = int(payload.get("limit") or 20)
        _set_task(
            db,
            task_id,
            status="running",
            stage="fetch",
            percent=10,
            total=limit,
            message="Fetching candidate minute bars",
            started_at=_now(),
        )
        summary = sync_tail_proxy_snapshots(
            db, source=source, limit=limit
        )
        db.commit()
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            total=summary.total,
            processed=summary.total,
            ok_count=summary.written,
            failed_count=summary.failed,
            message="Tail proxy snapshot completed",
            result_json=json.dumps(
                {
                    "trade_date": summary.trade_date,
                    "source_scope": summary.source_scope,
                    "total": summary.total,
                    "written": summary.written,
                    "skipped": summary.skipped,
                    "failed": summary.failed,
                    "errors": summary.errors,
                },
                ensure_ascii=False,
                default=str,
            ),
            errors_json=json.dumps(
                list(summary.errors), ensure_ascii=False
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Tail proxy task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            _append_error(task, {"scope": "tail_proxy", "error": str(exc)})
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


__all__ = ["TASK_TYPE", "create_tail_proxy_task"]
