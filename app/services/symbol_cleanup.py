"""定时清理挖掘任务遗留的僵尸标的。

覆盖场景：
  - paused 超过 RESUME_DEADLINE（24h）未续跑的任务
  - running 超过 STALE_RUNNING_DEADLINE（30m）僵死的任务
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discovery import DiscoveryTaskRecord
from app.services.discovery_tasks import (
    RESUME_DEADLINE,
    STALE_RUNNING_DEADLINE,
    _cleanup_discovery_symbols,
    _json_loads,
    _now,
    _payload_from_task,
)

logger = logging.getLogger(__name__)


def cleanup_stale_discovery_symbols(db: Session) -> dict:
    """清理暂停超时/僵死任务遗留的僵尸标的，返回清理统计。"""
    now = _now()
    cleaned_task_ids: list[str] = []
    total_cleaned = 0

    # 1. paused 超过 RESUME_DEADLINE（24h）
    stale_paused = db.execute(
        select(DiscoveryTaskRecord).where(
            DiscoveryTaskRecord.status == "paused",
            DiscoveryTaskRecord.paused_at.is_not(None),
            DiscoveryTaskRecord.paused_at < now - RESUME_DEADLINE,
        )
    ).scalars().all()

    for task in stale_paused:
        synced_ids = set(_json_loads(task.synced_symbol_ids_json, []))
        try:
            paused_scope = _payload_from_task(task).scope if task.payload_json else None
            cleaned = _cleanup_discovery_symbols(
                db, scoped_symbol_ids=synced_ids, preserve_extra_ids=None, scope=paused_scope,
            )
        except Exception:
            logger.exception("Failed to cleanup stale paused task %s", task.id)
            cleaned = 0
        if cleaned:
            total_cleaned += cleaned
            cleaned_task_ids.append(task.id)
            task.cleanup_count = cleaned
        # 标记任务为 expired，防止重复清理
        task.status = "expired"
        task.finished_at = task.finished_at or now
        db.commit()

    # 2. running 超过 STALE_RUNNING_DEADLINE（30m）僵死
    stale_running = db.execute(
        select(DiscoveryTaskRecord).where(
            DiscoveryTaskRecord.status == "running",
            DiscoveryTaskRecord.started_at.is_not(None),
            DiscoveryTaskRecord.started_at < now - STALE_RUNNING_DEADLINE,
        )
    ).scalars().all()

    for task in stale_running:
        synced_ids = set(_json_loads(task.synced_symbol_ids_json, []))
        try:
            running_scope = _payload_from_task(task).scope if task.payload_json else None
            cleaned = _cleanup_discovery_symbols(
                db, scoped_symbol_ids=synced_ids, preserve_extra_ids=None, scope=running_scope,
            )
        except Exception:
            logger.exception("Failed to cleanup stale running task %s", task.id)
            cleaned = 0
        if cleaned:
            total_cleaned += cleaned
            cleaned_task_ids.append(task.id)
            task.cleanup_count = cleaned
        task.status = "failed"
        task.message = "任务僵死超时，已自动清理"
        task.finished_at = task.finished_at or now
        db.commit()

    return {
        "cleaned_task_ids": cleaned_task_ids,
        "total_cleaned": total_cleaned,
    }
