"""通用异步任务服务层。

提供异步任务的创建、查询、取消等生命周期管理，
以及后台线程执行器的启动。任务状态持久化到 async_tasks 表。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import AsyncTaskRead
from app.db.session import get_session_local

logger = logging.getLogger(__name__)

# 超过 30 分钟无更新的 running/queued 任务自动标记为 failed
STALE_RUNNING_DEADLINE = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_loads(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to timestamps stored as legacy naive UTC values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _task_to_dict(task: AsyncTaskRecord) -> dict:
    """将 ORM 对象转为前端可用的字典，用于 AsyncTaskRead 构建。"""
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "stage": task.stage,
        "percent": round(task.percent, 1),
        "message": task.message or "",
        "total": task.total,
        "processed": task.processed,
        "ok_count": task.ok_count,
        "failed_count": task.failed_count,
        "current_item": task.current_item,
        "result": _json_loads(task.result_json, None),
        "errors": _json_loads(task.errors_json, [])[-20:],
        "created_at": _as_utc(task.created_at),
        "started_at": _as_utc(task.started_at),
        "finished_at": _as_utc(task.finished_at),
        "updated_at": _as_utc(task.updated_at),
    }


def _task_to_read(task: AsyncTaskRecord) -> AsyncTaskRead:
    return AsyncTaskRead.model_validate(_task_to_dict(task))


def _expire_stale_tasks(db: Session) -> None:
    """将超时未更新的任务标记为 failed。"""
    cutoff = _now() - STALE_RUNNING_DEADLINE
    rows = (
        db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.status.in_(("queued", "running")),
                AsyncTaskRecord.updated_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    for task in rows:
        task.status = "failed"
        task.stage = "failed"
        task.message = "Task expired (no update for 30 minutes)"
        task.finished_at = _now()
    if rows:
        db.commit()


def interrupt_orphaned_async_tasks(db: Session) -> list[str]:
    """Fail queued/running in-process tasks left behind by a backend restart."""
    rows = (
        db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.status.in_(("queued", "running"))
            )
        )
        .scalars()
        .all()
    )
    interrupted_at = _now()
    for task in rows:
        previous_stage = task.stage
        _append_error(
            task,
            {
                "code": "BACKEND_RESTART_INTERRUPTED",
                "stage": previous_stage,
                "error": "Backend restarted before the in-process worker completed",
            },
        )
        task.status = "failed"
        task.stage = "interrupted"
        task.message = "Backend restarted before task completed"
        task.finished_at = interrupted_at
        task.updated_at = interrupted_at
    if rows:
        db.commit()
    return [task.id for task in rows]


def _set_task(db: Session, task_id: str, **updates) -> AsyncTaskRecord:
    """原子更新任务字段并提交。如果任务已处于终态则不再覆盖 status/stage。"""
    task = db.get(AsyncTaskRecord, task_id)
    if task is None:
        raise ValueError(f"Async task not found: {task_id}")
    # 终态保护：已完成的任务不允许被覆盖回 running 等状态
    if task.status in ("done", "failed", "cancelled"):
        # 只允许更新非状态字段（如 updated_at），不覆盖 status/stage
        updates = {k: v for k, v in updates.items() if k not in ("status", "stage")}
        if not updates:
            return task
    for key, value in updates.items():
        setattr(task, key, value)
    task.updated_at = _now()
    db.commit()
    db.refresh(task)
    return task


def _append_error(task: AsyncTaskRecord, error: dict) -> None:
    """追加错误到 errors_json，保留最近 20 条。"""
    errors = _json_loads(task.errors_json, [])
    errors.append(error)
    task.errors_json = json.dumps(errors[-20:], ensure_ascii=False, default=str)


def _start_worker(task_id: str, worker_func) -> None:
    """启动守护线程执行 worker 函数。"""
    def _run():
        try:
            worker_func(task_id)
        except Exception:
            logger.exception("Worker crashed for task %s", task_id)
            # 尝试标记任务为 failed
            try:
                SessionLocal = get_session_local()
                db = SessionLocal()
                try:
                    task = db.get(AsyncTaskRecord, task_id)
                    if task and task.status not in ("done", "failed", "cancelled"):
                        task.status = "failed"
                        task.stage = "failed"
                        task.message = "Worker crashed unexpectedly"
                        task.finished_at = _now()
                        task.updated_at = _now()
                        db.commit()
                finally:
                    db.close()
            except Exception:
                logger.exception("Failed to mark task %s as failed after crash", task_id)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


# ── 公共 API ──────────────────────────────────────────────


def create_async_task(task_type: str, payload: dict) -> AsyncTaskRead:
    """创建异步任务并启动后台 worker（需调用方传入 worker_func 并自行调用 _start_worker）。

    此函数仅创建 DB 记录，不启动线程。
    返回任务快照供调用方立即响应前端。
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        _expire_stale_tasks(db)
        task_id = uuid4().hex
        task = AsyncTaskRecord(
            id=task_id,
            task_type=task_type,
            status="queued",
            stage="queued",
            percent=0,
            message="Task created",
            payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return _task_to_read(task)
    finally:
        db.close()


def get_async_task(task_id: str) -> AsyncTaskRead | None:
    """查询任务状态。"""
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        _expire_stale_tasks(db)
        task = db.get(AsyncTaskRecord, task_id)
        return _task_to_read(task) if task is not None else None
    finally:
        db.close()


def list_async_tasks(task_type: str | None = None, limit: int = 20) -> list[AsyncTaskRead]:
    """列出最近的异步任务。"""
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        _expire_stale_tasks(db)
        stmt = select(AsyncTaskRecord).order_by(desc(AsyncTaskRecord.created_at)).limit(limit)
        if task_type:
            stmt = stmt.where(AsyncTaskRecord.task_type == task_type)
        rows = db.execute(stmt).scalars().all()
        return [_task_to_read(item) for item in rows]
    finally:
        db.close()


def cancel_async_task(task_id: str) -> AsyncTaskRead:
    """取消任务。"""
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            raise ValueError("Async task not found")
        if task.status in ("done", "failed", "cancelled"):
            return _task_to_read(task)
        task.status = "cancelled"
        task.stage = "cancelled"
        task.message = "Task cancelled by user"
        task.finished_at = _now()
        task.updated_at = _now()
        db.commit()
        db.refresh(task)
        return _task_to_read(task)
    finally:
        db.close()
