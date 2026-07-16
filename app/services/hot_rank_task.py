"""Asynchronous wrapper for the current EastMoney hot-rank snapshot."""
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
from app.services.hot_rank_data import sync_hot_rank_snapshot


logger = logging.getLogger(__name__)
TASK_TYPE = "hot_rank_snapshot"


def create_hot_rank_task():
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {"queued", "running"}:
        return existing[0]
    task = create_async_task(TASK_TYPE, {})
    _start_worker(task.id, _run_hot_rank_task)
    return task


def _run_hot_rank_task(task_id: str) -> None:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        _set_task(
            db,
            task_id,
            status="running",
            stage="fetch",
            percent=10,
            message="Fetching current EastMoney hot rank",
            started_at=_now(),
        )
        summary = sync_hot_rank_snapshot(db)
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
            total=summary.received,
            processed=summary.received,
            ok_count=summary.written,
            failed_count=summary.unmatched,
            message="Hot-rank snapshot completed",
            result_json=json.dumps(
                {
                    "trade_date": summary.trade_date,
                    "received": summary.received,
                    "written": summary.written,
                    "unmatched": summary.unmatched,
                },
                ensure_ascii=False,
                default=str,
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Hot-rank task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            _append_error(task, {"scope": "hot_rank", "error": str(exc)})
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


__all__ = ["TASK_TYPE", "create_hot_rank_task"]
