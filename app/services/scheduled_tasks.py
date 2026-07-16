"""Persistent cross-platform scheduler and task dispatcher."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, desc, select, update
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.discovery import DiscoveryTaskRecord
from app.models.scheduled_task import (
    ScheduledTask,
    ScheduledTaskRun,
    ScheduledTaskSeedState,
)
from app.schemas.scheduled_task import ScheduledTaskCreate, ScheduledTaskUpdate


logger = logging.getLogger(__name__)
SCHEDULER_CHECK_INTERVAL_SECONDS = 30
DEFAULT_SCHEDULE_SEED_KEY = "default_schedules_v4"

TASK_DEFINITIONS: dict[str, dict] = {
    "universe_incremental_sync": {
        "name": "行情增量同步",
        "description": "增量同步基础股票池中已过期的日线行情",
        "default_payload": {
            "max_workers": 5,
            "scopes": ["cn-stock", "cn-etf", "us-stock", "us-etf"],
        },
    },
    "macro_update": {
        "name": "宏观数据更新",
        "description": "更新中美宏观指标、利率和流动性数据",
        "default_payload": {"region": "all"},
    },
    "factor_pipeline": {
        "name": "因子流水线",
        "description": "运行本地镜像、因子计算、标签、模型和评分快照",
        "default_payload": {
            "full_refresh": False,
            "train_model": False,
            "materialize_scores": True,
            "window_days": 250,
            "validation_days": 50,
        },
    },
    "hot_rank_snapshot": {
        "name": "东方财富人气榜快照",
        "description": "保存当前前100名人气榜，免费接口不支持历史回填",
        "default_payload": {},
    },
    "tail_proxy_snapshot": {
        "name": "候选池尾盘量价代理",
        "description": "抓取最新候选前20只的1分钟量价并计算尾盘代理",
        "default_payload": {"source": "candidates", "limit": 20},
    },
    "lhb_institution_sync": {
        "name": "龙虎榜机构席位同步",
        "description": "同步最近3日市场级机构席位买入、卖出和净额",
        "default_payload": {"lookback_days": 3},
    },
    "financial_report_sync": {
        "name": "财报历史同步",
        "description": "按公告日同步自选股财务分析历史，默认限制20只",
        "default_payload": {"source": "watchlist", "limit": 20},
    },
    "discovery_mining": {
        "name": "机会挖掘",
        "description": "使用本地股票池运行机会扫描和候选生成",
        "default_payload": {
            "scope": "cn-stock",
            "min_score": 55,
            "include_news": True,
            "batch_size": 20,
            "max_workers": 1,
            "use_cached_bars_first": True,
            "use_cached_symbols_only": True,
        },
    },
}

DEFAULT_SCHEDULES = (
    {
        "name": "每日行情增量同步",
        "task_type": "universe_incremental_sync",
        "frequency": "daily",
        "time_of_day": "18:00",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["universe_incremental_sync"]["default_payload"],
        "enabled": True,
    },
    {
        "name": "每日人气榜快照",
        "task_type": "hot_rank_snapshot",
        "frequency": "daily",
        "time_of_day": "16:20",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["hot_rank_snapshot"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每日候选尾盘代理",
        "task_type": "tail_proxy_snapshot",
        "frequency": "daily",
        "time_of_day": "15:10",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["tail_proxy_snapshot"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每日龙虎榜机构同步",
        "task_type": "lhb_institution_sync",
        "frequency": "daily",
        "time_of_day": "16:30",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["lhb_institution_sync"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每周财报历史同步",
        "task_type": "financial_report_sync",
        "frequency": "weekly",
        "time_of_day": "10:00",
        "weekdays": [5],
        "payload": TASK_DEFINITIONS["financial_report_sync"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每日宏观数据更新",
        "task_type": "macro_update",
        "frequency": "daily",
        "time_of_day": "17:30",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["macro_update"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每日因子评分",
        "task_type": "factor_pipeline",
        "frequency": "daily",
        "time_of_day": "18:10",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["factor_pipeline"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每周因子训练",
        "task_type": "factor_pipeline",
        "frequency": "weekly",
        "time_of_day": "18:30",
        "weekdays": [4],
        "payload": {
            **TASK_DEFINITIONS["factor_pipeline"]["default_payload"],
            "train_model": True,
        },
        "enabled": False,
    },
    {
        "name": "每日机会挖掘",
        "task_type": "discovery_mining",
        "frequency": "daily",
        "time_of_day": "18:40",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["discovery_mining"]["default_payload"],
        "enabled": False,
    },
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc


def calculate_next_run(
    *,
    frequency: str,
    time_of_day: str | None,
    weekdays: list[int],
    interval_minutes: int | None,
    timezone_name: str,
    after_utc: datetime | None = None,
) -> datetime:
    """Return the next run as a naive UTC datetime."""
    after = after_utc or _now()
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    else:
        after = after.astimezone(timezone.utc)
    zone = _zone(timezone_name)
    local_after = after.astimezone(zone)

    if frequency == "interval":
        if not interval_minutes:
            raise ValueError("interval_minutes is required")
        candidate = after + timedelta(minutes=interval_minutes)
        return candidate.astimezone(timezone.utc).replace(tzinfo=None)

    if not time_of_day:
        raise ValueError("time_of_day is required")
    hour, minute = (int(item) for item in time_of_day.split(":"))
    run_time = time(hour=hour, minute=minute)

    if frequency == "daily":
        candidate = datetime.combine(local_after.date(), run_time, tzinfo=zone)
        if candidate <= local_after:
            candidate += timedelta(days=1)
    elif frequency == "weekly":
        if not weekdays:
            raise ValueError("weekdays is required")
        candidate = None
        for offset in range(0, 8):
            day = local_after.date() + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            possible = datetime.combine(day, run_time, tzinfo=zone)
            if possible > local_after:
                candidate = possible
                break
        if candidate is None:
            raise ValueError("Unable to calculate weekly schedule")
    else:
        raise ValueError(f"Unsupported frequency: {frequency}")
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def _next_for_model(item: ScheduledTask, *, after: datetime | None = None) -> datetime:
    return calculate_next_run(
        frequency=item.frequency,
        time_of_day=item.time_of_day,
        weekdays=_json(item.weekdays_json, []),
        interval_minutes=item.interval_minutes,
        timezone_name=item.timezone,
        after_utc=after,
    )


def seed_default_schedules(db: Session) -> int:
    if db.get(ScheduledTaskSeedState, DEFAULT_SCHEDULE_SEED_KEY) is not None:
        return 0
    created = 0
    existing_names = set(db.execute(select(ScheduledTask.name)).scalars().all())
    for data in DEFAULT_SCHEDULES:
        if data["name"] in existing_names:
            continue
        payload = ScheduledTaskCreate(
            **data,
            timezone="Asia/Shanghai",
        )
        item = ScheduledTask(
            name=payload.name,
            task_type=payload.task_type,
            frequency=payload.frequency,
            time_of_day=payload.time_of_day,
            weekdays_json=json.dumps(payload.weekdays),
            interval_minutes=payload.interval_minutes,
            timezone=payload.timezone,
            payload_json=json.dumps(payload.payload, ensure_ascii=False),
            enabled=int(payload.enabled),
        )
        if payload.enabled:
            item.next_run_at = _next_for_model(item)
        db.add(item)
        created += 1
    db.add(ScheduledTaskSeedState(key=DEFAULT_SCHEDULE_SEED_KEY))
    db.flush()
    return created


def validate_definition(task_type: str) -> None:
    if task_type not in TASK_DEFINITIONS:
        raise ValueError(f"Unsupported scheduled task type: {task_type}")


def validate_task_payload(task_type: str, payload: dict) -> dict:
    """Validate task-specific settings when a schedule is saved."""
    validate_definition(task_type)
    if task_type == "universe_incremental_sync":
        allowed_scopes = {"cn-stock", "cn-etf", "us-stock", "us-etf"}
        scopes = payload.get("scopes") or sorted(allowed_scopes)
        if not isinstance(scopes, list) or not scopes:
            raise ValueError("scopes must be a non-empty list")
        if any(item not in allowed_scopes for item in scopes):
            raise ValueError("scopes contains an unsupported universe")
        max_workers = int(payload.get("max_workers", 5))
        if not 1 <= max_workers <= 20:
            raise ValueError("max_workers must be between 1 and 20")
        return {"max_workers": max_workers, "scopes": scopes}
    if task_type == "macro_update":
        from app.schemas.macro import MacroUpdateRequest

        return MacroUpdateRequest.model_validate(payload).model_dump(mode="json")
    if task_type == "hot_rank_snapshot":
        if payload:
            raise ValueError("hot_rank_snapshot payload must be empty")
        return {}
    if task_type == "tail_proxy_snapshot":
        source = str(payload.get("source") or "candidates")
        if source not in {"candidates", "watchlist", "positions"}:
            raise ValueError("unsupported tail proxy source")
        limit = int(payload.get("limit", 20))
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        return {"source": source, "limit": limit}
    if task_type == "lhb_institution_sync":
        lookback_days = int(payload.get("lookback_days", 3))
        if not 1 <= lookback_days <= 31:
            raise ValueError(
                "lookback_days must be between 1 and 31"
            )
        return {"lookback_days": lookback_days}
    if task_type == "financial_report_sync":
        source = str(payload.get("source") or "watchlist")
        if source not in {"watchlist", "positions", "all"}:
            raise ValueError("unsupported financial report source")
        limit = int(payload.get("limit", 20))
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return {"source": source, "limit": limit}
    if task_type == "factor_pipeline":
        from app.schemas.async_task import FactorPipelineCreate

        validated = FactorPipelineCreate.model_validate(payload)
        if validated.validation_days >= validated.window_days:
            raise ValueError("validation_days must be less than window_days")
        return validated.model_dump(mode="json")
    if task_type == "discovery_mining":
        from app.schemas.discovery import DiscoveryTaskCreate

        return DiscoveryTaskCreate.model_validate(payload).model_dump(mode="json")
    raise ValueError(f"Unsupported scheduled task type: {task_type}")


def create_schedule(db: Session, payload: ScheduledTaskCreate) -> ScheduledTask:
    validate_definition(payload.task_type)
    _zone(payload.timezone)
    task_payload = validate_task_payload(payload.task_type, payload.payload)
    item = ScheduledTask(
        name=payload.name,
        task_type=payload.task_type,
        frequency=payload.frequency,
        time_of_day=payload.time_of_day,
        weekdays_json=json.dumps(payload.weekdays),
        interval_minutes=payload.interval_minutes,
        timezone=payload.timezone,
        payload_json=json.dumps(task_payload, ensure_ascii=False, default=str),
        enabled=int(payload.enabled),
    )
    if payload.enabled:
        item.next_run_at = _next_for_model(item)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_schedule(
    db: Session,
    schedule_id: int,
    patch: ScheduledTaskUpdate,
) -> ScheduledTask:
    item = db.get(ScheduledTask, schedule_id)
    if item is None:
        raise ValueError("Scheduled task not found")
    current = {
        "name": item.name,
        "task_type": item.task_type,
        "frequency": item.frequency,
        "time_of_day": item.time_of_day,
        "weekdays": _json(item.weekdays_json, []),
        "interval_minutes": item.interval_minutes,
        "timezone": item.timezone,
        "payload": _json(item.payload_json, {}),
        "enabled": bool(item.enabled),
    }
    current.update(patch.model_dump(exclude_unset=True))
    validated = ScheduledTaskCreate.model_validate(current)
    validate_definition(validated.task_type)
    _zone(validated.timezone)
    task_payload = validate_task_payload(
        validated.task_type,
        validated.payload,
    )
    item.name = validated.name
    item.task_type = validated.task_type
    item.frequency = validated.frequency
    item.time_of_day = validated.time_of_day
    item.weekdays_json = json.dumps(validated.weekdays)
    item.interval_minutes = validated.interval_minutes
    item.timezone = validated.timezone
    item.payload_json = json.dumps(task_payload, ensure_ascii=False, default=str)
    item.enabled = int(validated.enabled)
    item.next_run_at = _next_for_model(item) if validated.enabled else None
    item.updated_at = _now()
    db.commit()
    db.refresh(item)
    return item


def delete_schedule(db: Session, schedule_id: int) -> None:
    item = db.get(ScheduledTask, schedule_id)
    if item is None:
        raise ValueError("Scheduled task not found")
    db.execute(delete(ScheduledTaskRun).where(ScheduledTaskRun.schedule_id == schedule_id))
    db.delete(item)
    db.commit()


def _dispatch_task(item: ScheduledTask):
    payload = _json(item.payload_json, {})
    if item.task_type == "universe_incremental_sync":
        from app.services.universe_sync_task import start_universe_incremental_sync

        task = start_universe_incremental_sync(
            max_workers=int(payload.get("max_workers", 5)),
            scopes=payload.get("scopes"),
        )
        return "async", task
    if item.task_type == "macro_update":
        from app.schemas.macro import MacroUpdateRequest
        from app.services.macro_update_task import create_macro_update_task

        task = create_macro_update_task(MacroUpdateRequest.model_validate(payload))
        return "async", task
    if item.task_type == "hot_rank_snapshot":
        from app.services.hot_rank_task import create_hot_rank_task

        return "async", create_hot_rank_task()
    if item.task_type == "tail_proxy_snapshot":
        from app.services.tail_proxy_task import create_tail_proxy_task

        return "async", create_tail_proxy_task(
            source=str(payload.get("source") or "candidates"),
            limit=int(payload.get("limit") or 20),
        )
    if item.task_type == "lhb_institution_sync":
        from app.services.lhb_institution_task import (
            create_lhb_institution_task,
        )

        return "async", create_lhb_institution_task(
            lookback_days=int(payload.get("lookback_days") or 3)
        )
    if item.task_type == "financial_report_sync":
        from app.services.financial_report_task import (
            create_financial_report_task,
        )

        return "async", create_financial_report_task(
            source=str(payload.get("source") or "watchlist"),
            limit=int(payload.get("limit") or 20),
        )
    if item.task_type == "factor_pipeline":
        from app.schemas.async_task import FactorPipelineCreate
        from app.services.factors.pipeline_task import create_factor_pipeline_task

        task = create_factor_pipeline_task(FactorPipelineCreate.model_validate(payload))
        return "async", task
    if item.task_type == "discovery_mining":
        from app.schemas.discovery import DiscoveryTaskCreate
        from app.services.discovery_tasks import create_discovery_task

        task = create_discovery_task(DiscoveryTaskCreate.model_validate(payload))
        return "discovery", task
    raise ValueError(f"Unsupported scheduled task type: {item.task_type}")


def execute_schedule(
    db: Session,
    schedule_id: int,
    *,
    trigger_source: str,
) -> ScheduledTaskRun:
    item = db.get(ScheduledTask, schedule_id)
    if item is None:
        raise ValueError("Scheduled task not found")
    run = ScheduledTaskRun(
        schedule_id=item.id,
        trigger_source=trigger_source,
        status="dispatching",
        message="Dispatching task",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    try:
        source, task = _dispatch_task(item)
        task_id = task.get("id") if isinstance(task, dict) else getattr(task, "id", None)
        task_status = task.get("status") if isinstance(task, dict) else getattr(task, "status", None)
        task_message = task.get("message") if isinstance(task, dict) else getattr(task, "message", None)
        if not task_id:
            raise ValueError("Task dispatcher returned no task id")
        run.task_source = source
        run.task_id = str(task_id)
        run.status = str(task_status or "queued")
        run.message = str(task_message or "Task dispatched")
        item.last_run_at = _now()
        item.last_status = run.status
        item.last_task_id = run.task_id
        item.last_error = None
        item.updated_at = _now()
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        run.status = "failed"
        run.message = str(exc)
        item.last_run_at = _now()
        item.last_status = "failed"
        item.last_error = str(exc)
        item.updated_at = _now()
        db.commit()
        logger.exception("Scheduled task %s dispatch failed", schedule_id)
        raise ValueError(str(exc)) from exc


def _task_state(db: Session, source: str | None, task_id: str | None):
    if not source or not task_id:
        return None
    if source == "async":
        return db.get(AsyncTaskRecord, task_id)
    if source == "discovery":
        return db.get(DiscoveryTaskRecord, task_id)
    return None


def schedule_to_dict(db: Session, item: ScheduledTask) -> dict:
    latest_run = db.execute(
        select(ScheduledTaskRun)
        .where(ScheduledTaskRun.schedule_id == item.id)
        .order_by(desc(ScheduledTaskRun.created_at), desc(ScheduledTaskRun.id))
        .limit(1)
    ).scalars().first()
    task = _task_state(
        db,
        latest_run.task_source if latest_run else None,
        latest_run.task_id if latest_run else None,
    )
    return {
        "id": item.id,
        "name": item.name,
        "task_type": item.task_type,
        "frequency": item.frequency,
        "time_of_day": item.time_of_day,
        "weekdays": _json(item.weekdays_json, []),
        "interval_minutes": item.interval_minutes,
        "timezone": item.timezone,
        "payload": _json(item.payload_json, {}),
        "enabled": bool(item.enabled),
        "next_run_at": item.next_run_at,
        "last_run_at": item.last_run_at,
        "last_status": item.last_status,
        "last_task_id": item.last_task_id,
        "last_task_status": getattr(task, "status", item.last_status),
        "last_message": getattr(task, "message", latest_run.message if latest_run else None),
        "last_error": item.last_error,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def run_to_dict(db: Session, run: ScheduledTaskRun) -> dict:
    task = _task_state(db, run.task_source, run.task_id)
    schedule = db.get(ScheduledTask, run.schedule_id)
    return {
        "id": run.id,
        "schedule_id": run.schedule_id,
        "schedule_name": schedule.name if schedule else None,
        "task_type": schedule.task_type if schedule else None,
        "trigger_source": run.trigger_source,
        "task_source": run.task_source,
        "task_id": run.task_id,
        "status": getattr(task, "status", run.status),
        "message": getattr(task, "message", run.message),
        "created_at": run.created_at,
        "finished_at": getattr(task, "finished_at", None),
    }


def run_due_schedules() -> int:
    SessionLocal = get_session_local()
    db = SessionLocal()
    claimed: list[int] = []
    now = _now()
    try:
        candidates = db.execute(
            select(ScheduledTask).where(
                ScheduledTask.enabled == 1,
                ScheduledTask.next_run_at.is_not(None),
                ScheduledTask.next_run_at <= now,
            ).order_by(ScheduledTask.next_run_at, ScheduledTask.id)
        ).scalars().all()
        for item in candidates:
            old_next = item.next_run_at
            next_run = _next_for_model(item, after=now + timedelta(seconds=1))
            result = db.execute(
                update(ScheduledTask)
                .where(
                    ScheduledTask.id == item.id,
                    ScheduledTask.enabled == 1,
                    ScheduledTask.next_run_at == old_next,
                )
                .values(next_run_at=next_run, updated_at=now)
            )
            db.commit()
            if result.rowcount == 1:
                claimed.append(item.id)
        for schedule_id in claimed:
            try:
                execute_schedule(db, schedule_id, trigger_source="scheduled")
            except ValueError:
                logger.warning("Scheduled task %s failed to dispatch", schedule_id)
        return len(claimed)
    finally:
        db.close()


async def scheduler_loop() -> None:
    """Poll due schedules while the FastAPI process is alive."""
    while True:
        try:
            await asyncio.to_thread(run_due_schedules)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled task loop failed")
        await asyncio.sleep(SCHEDULER_CHECK_INTERVAL_SECONDS)


__all__ = [
    "TASK_DEFINITIONS",
    "calculate_next_run",
    "create_schedule",
    "delete_schedule",
    "execute_schedule",
    "run_due_schedules",
    "run_to_dict",
    "schedule_to_dict",
    "scheduler_loop",
    "seed_default_schedules",
    "update_schedule",
]
