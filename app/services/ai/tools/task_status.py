"""get_task_status 工具：返回任务状态。"""
from __future__ import annotations

import logging

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack

logger = logging.getLogger(__name__)


def get_task_status(db: Session, context: ContextPack) -> dict:
    """返回任务状态：调度任务列表、运行状态、下次执行时间。

    Returns:
        {
            "status": "ok",
            "data": {
                "scheduled_tasks": [...],  # 启用的调度任务
                "recent_async_tasks": [...],  # 最近的异步任务
                "next_run_at": str|None,
            }
        }
        或 {"status": "no_data", "message": "..."}
    """
    # 调度任务
    scheduled: list[dict] = []
    try:
        from app.models.scheduled_task import ScheduledTask
        rows = db.execute(
            select(ScheduledTask)
            .where(ScheduledTask.enabled == 1)
            .order_by(ScheduledTask.next_run_at.asc().nulls_last())
            .limit(20)
        ).scalars().all()
        for t in rows:
            scheduled.append({
                "id": t.id,
                "name": t.name,
                "task_type": t.task_type,
                "frequency": t.frequency,
                "time_of_day": t.time_of_day,
                "interval_minutes": t.interval_minutes,
                "next_run_at": t.next_run_at.isoformat() if t.next_run_at else None,
                "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
                "last_status": t.last_status,
                "last_error": t.last_error,
            })
    except Exception as exc:
        logger.warning("get_task_status scheduled query failed: %s", exc)

    # 最近异步任务
    recent_async: list[dict] = []
    try:
        from app.models.async_task import AsyncTaskRecord
        rows = db.execute(
            select(AsyncTaskRecord)
            .order_by(desc(AsyncTaskRecord.created_at))
            .limit(10)
        ).scalars().all()
        for t in rows:
            recent_async.append({
                "id": t.id,
                "task_type": t.task_type,
                "status": t.status,
                "stage": t.stage,
                "percent": float(t.percent) if t.percent is not None else 0.0,
                "message": t.message,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            })
    except Exception as exc:
        logger.warning("get_task_status async query failed: %s", exc)

    if not scheduled and not recent_async:
        return {"status": "no_data", "message": "无可用任务记录"}

    # 最近一次 next_run_at
    next_run_at = None
    for t in scheduled:
        if t.get("next_run_at"):
            next_run_at = t["next_run_at"]
            break

    return {
        "status": "ok",
        "data": {
            "scheduled_tasks": scheduled,
            "recent_async_tasks": recent_async,
            "next_run_at": next_run_at,
        },
    }


__all__ = ["get_task_status"]
