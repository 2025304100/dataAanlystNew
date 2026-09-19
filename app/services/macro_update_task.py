from __future__ import annotations

import json
import logging
from typing import Any

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import AsyncTaskRead
from app.schemas.macro import MacroUpdateRequest
from app.services.async_tasks import (
    _append_error,
    _now,
    _set_task,
    _start_worker,
    cancel_async_task,
    create_async_task,
    get_async_task,
    list_async_tasks,
)
from app.services.macro import SPECS, update_macro_data

logger = logging.getLogger(__name__)

TASK_TYPE = "macro_update"


def _check_cancelled(db, task_id: str) -> bool:
    task = db.get(AsyncTaskRecord, task_id)
    return task is not None and task.status == "cancelled"


def _calc_percent(processed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(min(processed / total, 1.0) * 95, 1)


def create_macro_update_task(payload: MacroUpdateRequest) -> AsyncTaskRead:
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in ("queued", "running"):
        return existing[0]
    task = create_async_task(TASK_TYPE, payload.model_dump())
    _start_worker(task.id, _run_macro_update)
    return task


def get_macro_update_task(task_id: str) -> AsyncTaskRead | None:
    task = get_async_task(task_id)
    if task is None or task.task_type != TASK_TYPE:
        return None
    return task


def get_latest_macro_update_task() -> AsyncTaskRead | None:
    tasks = list_async_tasks(task_type=TASK_TYPE, limit=1)
    return tasks[0] if tasks else None


def cancel_macro_update_task(task_id: str) -> AsyncTaskRead | None:
    task = get_macro_update_task(task_id)
    if task is None:
        return None
    return cancel_async_task(task_id)


def _update_task_progress(task_id: str, progress: dict[str, Any]) -> bool:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return False
        processed = int(progress.get("processed") or 0)
        total = int(progress.get("total") or 0)
        _set_task(
            db,
            task_id,
            stage=str(progress.get("stage") or "fetch"),
            percent=100 if progress.get("stage") == "done" else _calc_percent(processed, total),
            processed=processed,
            total=total,
            ok_count=int(progress.get("ok_count") or 0),
            failed_count=int(progress.get("failed_count") or 0),
            current_item=progress.get("current_item"),
            message=str(progress.get("message") or "Updating macro data"),
        )
        return True
    finally:
        db.close()


def _run_macro_update(task_id: str) -> None:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return
        payload = MacroUpdateRequest.model_validate(json.loads(task.payload_json or "{}"))
        total = len([item for item in SPECS if payload.region == "all" or item.region == payload.region])
        _set_task(
            db,
            task_id,
            status="running",
            stage="fetch",
            percent=0,
            total=total,
            processed=0,
            ok_count=0,
            failed_count=0,
            message="Starting macro update",
            started_at=_now(),
        )
        if _check_cancelled(db, task_id):
            return

        overview = update_macro_data(
            db,
            region=payload.region,
            progress_callback=lambda progress: _update_task_progress(task_id, progress),
        )
        if _check_cancelled(db, task_id):
            return
        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            total=total,
            processed=total,
            ok_count=max(total - len(overview.failed), 0),
            failed_count=len(overview.failed),
            current_item=None,
            message="Macro update completed",
            result_json=json.dumps(overview.model_dump(mode="json"), ensure_ascii=False, default=str),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Macro update task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            _append_error(task, {"scope": "macro", "error": str(exc)})
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()
