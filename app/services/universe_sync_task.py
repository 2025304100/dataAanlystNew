"""基础数据初始化同步的异步任务封装。

复用 AsyncTaskRecord 模型（task_type="universe_sync"），支持进度跟踪、取消、重试。
断点续传通过 universe_symbols.is_synced 字段天然实现：重试时跳过已同步标的。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, get_session_local
from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import AsyncTaskRead
from app.services import universe_sync

logger = logging.getLogger(__name__)

# 超过 60 分钟无更新的 running 任务标记为 failed（全量同步可能较久，给足时间）
_STALE_DEADLINE_SECONDS = 60 * 60

UNIVERSE_SYNC_TASK_TYPE = "universe_sync"
UNIVERSE_INCREMENTAL_TASK_TYPE = "universe_incremental_sync"
UNIVERSE_BACKFILL_TASK_TYPE = "universe_backfill"
UNIVERSE_SMART_TASK_TYPE = "universe_smart_sync"
UNIVERSE_RANGE_REPAIR_TASK_TYPE = "universe_range_repair"

_ALL_UNIVERSE_SCOPES = ["cn-stock", "cn-etf", "us-stock", "us-etf"]
_UNIVERSE_SCOPE_LABELS = {
    "cn-stock": "A股股票",
    "cn-etf": "A股ETF",
    "us-stock": "美股股票",
    "us-etf": "美股ETF",
}

# 进度阶段百分比分配（6 个 sync 阶段 + 4 个 refresh 阶段）
# A股股票数量最多（~5500），给最大比例区间
_STAGE_RANGES = {
    "refresh_stock": (0, 3),       # 拉取 A股股票列表
    "refresh_etf": (3, 5),         # 拉取 A股ETF列表
    "refresh_us_stock": (5, 7),    # 拉取 美股股票列表
    "refresh_us_etf": (7, 8),      # 拉取 美股ETF列表
    "sync_stock": (8, 45),         # 同步 A股股票K线（最多）
    "sync_etf": (45, 70),          # 同步 A股ETF K线
    "sync_us_stock": (70, 95),     # 同步 美股股票K线
    "sync_us_etf": (95, 100),      # 同步 美股ETF K线
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_loads(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _task_to_dict(task: AsyncTaskRecord) -> dict:
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
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "updated_at": task.updated_at,
    }


def _task_to_read(task: AsyncTaskRecord) -> AsyncTaskRead:
    return AsyncTaskRead.model_validate(_task_to_dict(task))


def _set_task(db: Session, task_id: str, **updates) -> AsyncTaskRecord | None:
    """原子更新任务字段并提交。终态保护：不覆盖 done/failed/cancelled 的 status/stage。"""
    task = db.get(AsyncTaskRecord, task_id)
    if task is None:
        return None
    if task.status in ("done", "failed", "cancelled"):
        updates = {k: v for k, v in updates.items() if k not in ("status", "stage")}
        if not updates:
            return task
    for key, value in updates.items():
        setattr(task, key, value)
    task.updated_at = _now()
    db.commit()
    db.refresh(task)
    return task


def _append_error(db: Session, task_id: str, error: dict) -> None:
    task = db.get(AsyncTaskRecord, task_id)
    if task is None:
        return
    errors = _json_loads(task.errors_json, [])
    errors.append(error)
    task.errors_json = json.dumps(errors[-20:], ensure_ascii=False, default=str)
    task.updated_at = _now()
    db.commit()


def _is_cancelled(task_id: str) -> bool:
    """检查任务是否被取消（worker 线程调用）。"""
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return True
        return task.status == "cancelled"
    finally:
        db.close()


def _start_task_heartbeat(
    session_factory,
    task_id: str,
    interval_seconds: float = 30.0,
) -> tuple[threading.Event, threading.Thread]:
    """Keep a long-running in-process task alive between progress callbacks."""
    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(interval_seconds):
            db = session_factory()
            try:
                task = db.get(AsyncTaskRecord, task_id)
                if task is None or task.status in ("done", "failed", "cancelled"):
                    return
                task.updated_at = _now()
                db.commit()
            except Exception:
                logger.exception("Heartbeat update failed for universe task %s", task_id)
                db.rollback()
            finally:
                db.close()

    thread = threading.Thread(
        target=_heartbeat,
        name=f"universe-heartbeat-{task_id[:8]}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _make_progress_callback(db_session_factory, task_id: str, stage: str, range_start: float, range_end: float, base_message: str):
    """创建进度回调闭包，将子任务进度映射到全局百分比区间。"""

    def callback(processed: int, total: int, ok: int, failed: int):
        if total <= 0:
            percent = range_end
        else:
            percent = range_start + (range_end - range_start) * processed / total
        db = db_session_factory()
        try:
            _set_task(
                db, task_id,
                stage=stage,
                percent=min(percent, range_end),
                total=total,
                processed=processed,
                ok_count=ok,
                failed_count=failed,
                message=f"{base_message}（{processed}/{total}）",
            )
        finally:
            db.close()

    return callback


# ── 公共 API ──────────────────────────────────────────────

def start_universe_sync_init(
    max_workers: int = 5,
    history_days: int = 365,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> AsyncTaskRead:
    """启动基础数据初始化同步任务。

    P0.6：支持通过 scopes 参数指定只初始化部分 scope（如 ["cn-stock","cn-etf"]），
    None 则初始化全部（cn-stock + cn-etf + us-stock + us-etf）。

    若已有 running/queued 任务则拒绝重复创建。
    """
    if scopes is None:
        scopes = list(_ALL_UNIVERSE_SCOPES)
    db = SessionLocal()
    try:
        # 并发保护：已有运行中任务则拒绝
        existing = db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.task_type.in_((
                    UNIVERSE_SYNC_TASK_TYPE,
                    UNIVERSE_INCREMENTAL_TASK_TYPE,
                    UNIVERSE_BACKFILL_TASK_TYPE,
                    UNIVERSE_SMART_TASK_TYPE,
                    UNIVERSE_RANGE_REPAIR_TASK_TYPE,
                )),
                AsyncTaskRecord.status.in_(("queued", "running")),
            ).order_by(desc(AsyncTaskRecord.created_at))
        ).scalars().first()
        if existing is not None:
            return _task_to_read(existing)

        task_id = uuid4().hex
        scope_label = "+".join(scopes) if len(scopes) < 4 else "全部"
        task = AsyncTaskRecord(
            id=task_id,
            task_type=UNIVERSE_SYNC_TASK_TYPE,
            status="queued",
            stage="queued",
            percent=0,
            message=f"初始化同步任务已创建（范围：{scope_label}）",
            payload_json=json.dumps(
                {
                    "max_workers": max_workers,
                    "history_days": history_days,
                    "mode": "init",
                    "scopes": scopes,
                    "sync_limit": sync_limit,
                },
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        result = _task_to_read(task)
    finally:
        db.close()

    # 启动 worker 线程
    worker = threading.Thread(
        target=_run_universe_sync_init,
        args=(task_id, max_workers, history_days, scopes, sync_limit),
        daemon=True,
    )
    worker.start()
    return result


def retry_universe_sync_init(
    max_workers: int = 5,
    history_days: int = 365,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> AsyncTaskRead:
    """重试初始化同步（跳过已同步标的，天然断点续传）。

    若已有运行中任务则拒绝。
    """
    return start_universe_sync_init(
        max_workers=max_workers, history_days=history_days, scopes=scopes, sync_limit=sync_limit
    )


def get_universe_sync_task(task_id: str) -> AsyncTaskRead | None:
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        return _task_to_read(task) if task is not None else None
    finally:
        db.close()


def get_latest_universe_sync_task() -> AsyncTaskRead | None:
    """获取最新的 universe_sync 任务。"""
    db = SessionLocal()
    try:
        task = db.execute(
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.task_type == UNIVERSE_SYNC_TASK_TYPE)
            .order_by(desc(AsyncTaskRecord.created_at))
            .limit(1)
        ).scalars().first()
        return _task_to_read(task) if task is not None else None
    finally:
        db.close()


def cancel_universe_sync_task(task_id: str) -> AsyncTaskRead | None:
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return None
        if task.status in ("done", "failed", "cancelled"):
            return _task_to_read(task)
        task.status = "cancelled"
        task.stage = "cancelled"
        task.message = "任务已取消（已同步的标的不会丢失，重试可继续）"
        task.finished_at = _now()
        task.updated_at = _now()
        db.commit()
        db.refresh(task)
        return _task_to_read(task)
    finally:
        db.close()


# ── worker 主函数 ─────────────────────────────────────────

def _run_universe_sync_init(
    task_id: str,
    max_workers: int,
    history_days: int,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> None:
    """worker 线程主函数：执行全量初始化同步（A股+美股 股票+ETF）。

    P0.6：scopes 指定要同步的 scope 列表，未包含的 scope 跳过对应阶段。
    sync_limit：单次同步标的上限（0=不限），用于分段同步。
    进度百分比沿用全局区间，跳过的阶段直接跳到下个阶段的起点。
    """
    if scopes is None:
        scopes = ["cn-stock", "cn-etf", "us-stock", "us-etf"]
    SessionFactory = get_session_local()
    errors: list[dict] = []
    result_summary: dict[str, Any] = {}

    try:
        # 标记 running
        db = SessionFactory()
        try:
            _set_task(
                db, task_id,
                status="running",
                stage="refresh_stock",
                percent=0,
                message="开始初始化同步",
                started_at=_now(),
            )
        finally:
            db.close()

        if _is_cancelled(task_id):
            return

        # 阶段1: refresh cn-stock universe
        if "cn-stock" in scopes:
            stage_range = _STAGE_RANGES["refresh_stock"]
            db = SessionFactory()
            try:
                _set_task(db, task_id, stage="refresh_stock", percent=stage_range[0], message="开始拉取 A股股票列表")
            finally:
                db.close()
            db = SessionFactory()
            try:
                try:
                    stock_result = universe_sync.refresh_universe_symbols("cn-stock", db)
                    result_summary["cn_stock_universe"] = stock_result
                except Exception as exc:
                    logger.exception("refresh cn-stock universe failed")
                    errors.append({"stage": "refresh_stock", "error": str(exc)})
                    _append_error(db, task_id, {"stage": "refresh_stock", "error": str(exc)})
            finally:
                db.close()
        else:
            logger.info("universe init skip cn-stock (not in scopes)")

        if _is_cancelled(task_id):
            return

        # 阶段2: refresh cn-etf universe
        if "cn-etf" in scopes:
            stage_range = _STAGE_RANGES["refresh_etf"]
            db = SessionFactory()
            try:
                _set_task(db, task_id, stage="refresh_etf", percent=stage_range[0], message="开始拉取 A股ETF列表")
            finally:
                db.close()
            db = SessionFactory()
            try:
                try:
                    etf_result = universe_sync.refresh_universe_symbols("cn-etf", db)
                    result_summary["cn_etf_universe"] = etf_result
                except Exception as exc:
                    logger.exception("refresh cn-etf universe failed")
                    errors.append({"stage": "refresh_etf", "error": str(exc)})
                    _append_error(db, task_id, {"stage": "refresh_etf", "error": str(exc)})
            finally:
                db.close()
        else:
            logger.info("universe init skip cn-etf (not in scopes)")

        if _is_cancelled(task_id):
            return

        # 阶段3: refresh us-stock universe
        if "us-stock" in scopes:
            stage_range = _STAGE_RANGES["refresh_us_stock"]
            db = SessionFactory()
            try:
                _set_task(db, task_id, stage="refresh_us_stock", percent=stage_range[0], message="开始拉取 美股股票列表")
            finally:
                db.close()
            db = SessionFactory()
            try:
                try:
                    us_stock_result = universe_sync.refresh_universe_symbols("us-stock", db)
                    result_summary["us_stock_universe"] = us_stock_result
                except Exception as exc:
                    logger.exception("refresh us-stock universe failed")
                    errors.append({"stage": "refresh_us_stock", "error": str(exc)})
                    _append_error(db, task_id, {"stage": "refresh_us_stock", "error": str(exc)})
            finally:
                db.close()
        else:
            logger.info("universe init skip us-stock (not in scopes)")

        if _is_cancelled(task_id):
            return

        # 阶段4: refresh us-etf universe
        if "us-etf" in scopes:
            stage_range = _STAGE_RANGES["refresh_us_etf"]
            db = SessionFactory()
            try:
                _set_task(db, task_id, stage="refresh_us_etf", percent=stage_range[0], message="开始拉取 美股ETF列表")
            finally:
                db.close()
            db = SessionFactory()
            try:
                try:
                    us_etf_result = universe_sync.refresh_universe_symbols("us-etf", db)
                    result_summary["us_etf_universe"] = us_etf_result
                except Exception as exc:
                    logger.exception("refresh us-etf universe failed")
                    errors.append({"stage": "refresh_us_etf", "error": str(exc)})
                    _append_error(db, task_id, {"stage": "refresh_us_etf", "error": str(exc)})
            finally:
                db.close()
        else:
            logger.info("universe init skip us-etf (not in scopes)")

        if _is_cancelled(task_id):
            return

        # 阶段5: sync cn-stock bars
        if "cn-stock" in scopes:
            stage_range = _STAGE_RANGES["sync_stock"]
            db = SessionFactory()
            try:
                _set_task(db, task_id, stage="sync_stock", percent=stage_range[0], message="开始同步 A股股票K线")
            finally:
                db.close()
            stock_progress_cb = _make_progress_callback(
                SessionFactory, task_id, "sync_stock", stage_range[0], stage_range[1], "同步A股股票K线",
            )
            try:
                stock_sync_result = universe_sync.sync_universe_bars_batch(
                    "cn-stock", max_workers=max_workers, history_days=history_days,
                    progress_callback=stock_progress_cb, is_cancelled=lambda: _is_cancelled(task_id),
                    sync_limit=sync_limit,
                )
                result_summary["cn_stock_bars"] = stock_sync_result
            except Exception as exc:
                logger.exception("sync cn-stock bars failed")
                errors.append({"stage": "sync_stock", "error": str(exc)})
                db = SessionFactory()
                try:
                    _append_error(db, task_id, {"stage": "sync_stock", "error": str(exc)})
                finally:
                    db.close()
        else:
            logger.info("universe init skip sync cn-stock bars (not in scopes)")

        if _is_cancelled(task_id):
            return

        # 阶段6: sync cn-etf bars
        if "cn-etf" in scopes:
            stage_range = _STAGE_RANGES["sync_etf"]
            db = SessionFactory()
            try:
                _set_task(db, task_id, stage="sync_etf", percent=stage_range[0], message="开始同步 A股ETF K线")
            finally:
                db.close()
            etf_progress_cb = _make_progress_callback(
                SessionFactory, task_id, "sync_etf", stage_range[0], stage_range[1], "同步A股ETF K线",
            )
            try:
                etf_sync_result = universe_sync.sync_universe_bars_batch(
                    "cn-etf", max_workers=max_workers, history_days=history_days,
                    progress_callback=etf_progress_cb, is_cancelled=lambda: _is_cancelled(task_id),
                    sync_limit=sync_limit,
                )
                result_summary["cn_etf_bars"] = etf_sync_result
            except Exception as exc:
                logger.exception("sync cn-etf bars failed")
                errors.append({"stage": "sync_etf", "error": str(exc)})
                db = SessionFactory()
                try:
                    _append_error(db, task_id, {"stage": "sync_etf", "error": str(exc)})
                finally:
                    db.close()
        else:
            logger.info("universe init skip sync cn-etf bars (not in scopes)")

        if _is_cancelled(task_id):
            return

        # 阶段7: sync us-stock bars
        if "us-stock" in scopes:
            stage_range = _STAGE_RANGES["sync_us_stock"]
            db = SessionFactory()
            try:
                _set_task(db, task_id, stage="sync_us_stock", percent=stage_range[0], message="开始同步 美股股票K线")
            finally:
                db.close()
            us_stock_progress_cb = _make_progress_callback(
                SessionFactory, task_id, "sync_us_stock", stage_range[0], stage_range[1], "同步美股股票K线",
            )
            try:
                us_stock_sync_result = universe_sync.sync_universe_bars_batch(
                    "us-stock", max_workers=max_workers, history_days=history_days,
                    progress_callback=us_stock_progress_cb, is_cancelled=lambda: _is_cancelled(task_id),
                    sync_limit=sync_limit,
                )
                result_summary["us_stock_bars"] = us_stock_sync_result
            except Exception as exc:
                logger.exception("sync us-stock bars failed")
                errors.append({"stage": "sync_us_stock", "error": str(exc)})
                db = SessionFactory()
                try:
                    _append_error(db, task_id, {"stage": "sync_us_stock", "error": str(exc)})
                finally:
                    db.close()
        else:
            logger.info("universe init skip sync us-stock bars (not in scopes)")

        if _is_cancelled(task_id):
            return

        # 阶段8: sync us-etf bars
        if "us-etf" in scopes:
            stage_range = _STAGE_RANGES["sync_us_etf"]
            db = SessionFactory()
            try:
                _set_task(db, task_id, stage="sync_us_etf", percent=stage_range[0], message="开始同步 美股ETF K线")
            finally:
                db.close()
            us_etf_progress_cb = _make_progress_callback(
                SessionFactory, task_id, "sync_us_etf", stage_range[0], stage_range[1], "同步美股ETF K线",
            )
            try:
                us_etf_sync_result = universe_sync.sync_universe_bars_batch(
                    "us-etf", max_workers=max_workers, history_days=history_days,
                    progress_callback=us_etf_progress_cb, is_cancelled=lambda: _is_cancelled(task_id),
                    sync_limit=sync_limit,
                )
                result_summary["us_etf_bars"] = us_etf_sync_result
            except Exception as exc:
                logger.exception("sync us-etf bars failed")
                errors.append({"stage": "sync_us_etf", "error": str(exc)})
                db = SessionFactory()
                try:
                    _append_error(db, task_id, {"stage": "sync_us_etf", "error": str(exc)})
                finally:
                    db.close()
        else:
            logger.info("universe init skip sync us-etf bars (not in scopes)")

        # 完成
        db = SessionFactory()
        try:
            final_status = "cancelled" if _is_cancelled(task_id) else "done"
            final_stage = "cancelled" if final_status == "cancelled" else "done"
            final_message = (
                "初始化同步完成" if final_status == "done"
                else "任务已取消（已同步进度已保留，可重试继续）"
            )
            final_updates: dict[str, Any] = {
                "status": final_status,
                "stage": final_stage,
                "message": final_message,
                "result_json": json.dumps(result_summary, ensure_ascii=False, default=str),
                "finished_at": _now(),
            }
            if final_status == "done":
                final_updates["percent"] = 100
            _set_task(db, task_id, **final_updates)
        finally:
            db.close()

    except Exception:
        logger.exception("universe sync init worker crashed: %s", task_id)
        try:
            db = SessionFactory()
            try:
                _set_task(
                    db, task_id,
                    status="failed",
                    stage="failed",
                    message="同步任务异常崩溃",
                    finished_at=_now(),
                )
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to mark universe sync task %s as failed", task_id)


# ── P2：定时增量同步任务封装 ─────────────────────────────────────

def start_universe_incremental_sync(
    max_workers: int = 5,
    scopes: list[str] | None = None,
) -> AsyncTaskRead:
    """启动增量同步任务（只同步 last_bar_date < today 的标的）。

    并发保护：已有 running/queued 的 universe 同步任务则拒绝。
    """
    if scopes is None:
        scopes = list(_ALL_UNIVERSE_SCOPES)
    db = SessionLocal()
    try:
        # 并发保护：已有运行中的 universe 同步任务则拒绝
        existing = db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.task_type.in_((
                    UNIVERSE_SYNC_TASK_TYPE,
                    UNIVERSE_INCREMENTAL_TASK_TYPE,
                    UNIVERSE_BACKFILL_TASK_TYPE,
                    UNIVERSE_SMART_TASK_TYPE,
                    UNIVERSE_RANGE_REPAIR_TASK_TYPE,
                )),
                AsyncTaskRecord.status.in_(("queued", "running")),
            ).order_by(desc(AsyncTaskRecord.created_at))
        ).scalars().first()
        if existing is not None:
            return _task_to_read(existing)

        task_id = uuid4().hex
        scope_label = "+".join(scopes) if len(scopes) < len(_ALL_UNIVERSE_SCOPES) else "全部"
        task = AsyncTaskRecord(
            id=task_id,
            task_type=UNIVERSE_INCREMENTAL_TASK_TYPE,
            status="queued",
            stage="queued",
            percent=0,
            message=f"增量同步任务已创建（范围：{scope_label}）",
            payload_json=json.dumps(
                {"max_workers": max_workers, "mode": "incremental", "scopes": scopes},
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        result = _task_to_read(task)
    finally:
        db.close()

    worker = threading.Thread(
        target=_run_universe_incremental_sync,
        args=(task_id, max_workers, scopes),
        daemon=True,
    )
    worker.start()
    return result


def get_latest_universe_incremental_task() -> AsyncTaskRead | None:
    """获取最新的增量同步任务。"""
    db = SessionLocal()
    try:
        task = db.execute(
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.task_type == UNIVERSE_INCREMENTAL_TASK_TYPE)
            .order_by(desc(AsyncTaskRecord.created_at))
            .limit(1)
        ).scalars().first()
        return _task_to_read(task) if task is not None else None
    finally:
        db.close()


def cancel_universe_incremental_task(task_id: str) -> AsyncTaskRead | None:
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return None
        if task.status in ("done", "failed", "cancelled"):
            return _task_to_read(task)
        task.status = "cancelled"
        task.stage = "cancelled"
        task.message = "增量同步任务已取消"
        task.finished_at = _now()
        task.updated_at = _now()
        db.commit()
        db.refresh(task)
        return _task_to_read(task)
    finally:
        db.close()


def _run_universe_incremental_sync(
    task_id: str,
    max_workers: int,
    scopes: list[str] | None = None,
) -> None:
    """worker 线程主函数：执行增量同步。"""
    SessionFactory = get_session_local()
    result_summary: dict[str, Any] = {}

    try:
        # 标记 running
        db = SessionFactory()
        try:
            _set_task(
                db, task_id,
                status="running",
                stage="sync_incremental",
                percent=0,
                message="开始增量同步（仅更新过期标的）",
                started_at=_now(),
            )
        finally:
            db.close()

        if _is_cancelled(task_id):
            return

        # 增量同步：单一阶段 0-100%
        def progress_cb(processed: int, total: int, ok: int, failed: int) -> None:
            if total <= 0:
                return
            pct = round(min(100, processed / total * 100), 1)
            try:
                d = SessionFactory()
                try:
                    _set_task(
                        d, task_id,
                        percent=pct,
                        total=total,
                        processed=processed,
                        ok_count=ok,
                        failed_count=failed,
                        message=f"增量同步中 {processed}/{total}（成功 {ok}，失败 {failed}）",
                    )
                finally:
                    d.close()
            except Exception:
                logger.debug("incremental_sync progress callback failed", exc_info=True)

        try:
            sync_result = universe_sync.incremental_sync(
                max_workers=max_workers,
                progress_callback=progress_cb,
                is_cancelled=lambda: _is_cancelled(task_id),
                scopes=scopes,
            )
            result_summary = sync_result
        except Exception as exc:
            logger.exception("incremental sync failed")
            db = SessionFactory()
            try:
                _append_error(db, task_id, {"stage": "sync_incremental", "error": str(exc)})
            finally:
                db.close()
            result_summary = {"error": str(exc)}

        # 完成
        db = SessionFactory()
        try:
            final_status = "cancelled" if _is_cancelled(task_id) else "done"
            final_stage = "cancelled" if final_status == "cancelled" else "done"
            ok = result_summary.get("ok", 0)
            failed = result_summary.get("failed", 0)
            uptodate = result_summary.get("uptodate", 0)
            final_message = (
                f"增量同步完成：更新 {ok} 个，已是最新 {uptodate} 个，失败 {failed} 个"
                if final_status == "done"
                else "增量同步任务已取消"
            )
            final_updates: dict[str, Any] = {
                "status": final_status,
                "stage": final_stage,
                "message": final_message,
                "result_json": json.dumps(result_summary, ensure_ascii=False, default=str),
                "finished_at": _now(),
            }
            if final_status == "done":
                final_updates["percent"] = 100
            _set_task(db, task_id, **final_updates)
        finally:
            db.close()

    except Exception:
        logger.exception("universe incremental sync worker crashed: %s", task_id)
        try:
            db = SessionFactory()
            try:
                _set_task(
                    db, task_id,
                    status="failed",
                    stage="failed",
                    message="增量同步任务异常崩溃",
                    finished_at=_now(),
                )
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to mark incremental sync task %s as failed", task_id)


# ── 智能同步任务 ─────────────────────────────────────────

_SMART_PHASE_RANGES = {
    "refresh": (0, 10),
    "init": (10, 55),
    "backfill": (55, 85),
    "incremental": (85, 100),
}


def _smart_stage_scope_range(scopes: list[str], scope: str, phase: str) -> tuple[float, float]:
    phase_start, phase_end = _SMART_PHASE_RANGES[phase]
    if not scopes:
        return phase_start, phase_end
    index = scopes.index(scope)
    width = (phase_end - phase_start) / max(len(scopes), 1)
    return phase_start + index * width, phase_start + (index + 1) * width


def start_universe_smart_sync(
    max_workers: int = 5,
    history_days: int = 365,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> AsyncTaskRead:
    """启动智能同步任务。

    自动按当前覆盖情况处理：
    1. 刷新标的列表
    2. 初始化未同步标的
    3. 仅向左侧补历史缺口
    4. 仅向右侧补近期增量
    """
    if scopes is None:
        scopes = list(_ALL_UNIVERSE_SCOPES)
    db = SessionLocal()
    try:
        existing = db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.task_type.in_((
                    UNIVERSE_SYNC_TASK_TYPE,
                    UNIVERSE_INCREMENTAL_TASK_TYPE,
                    UNIVERSE_BACKFILL_TASK_TYPE,
                    UNIVERSE_SMART_TASK_TYPE,
                    UNIVERSE_RANGE_REPAIR_TASK_TYPE,
                )),
                AsyncTaskRecord.status.in_(("queued", "running")),
            ).order_by(desc(AsyncTaskRecord.created_at))
        ).scalars().first()
        if existing is not None:
            return _task_to_read(existing)

        task_id = uuid4().hex
        scope_label = "+".join(scopes) if len(scopes) < len(_ALL_UNIVERSE_SCOPES) else "全部"
        task = AsyncTaskRecord(
            id=task_id,
            task_type=UNIVERSE_SMART_TASK_TYPE,
            status="queued",
            stage="queued",
            percent=0,
            message=f"智能同步任务已创建（范围：{scope_label}，历史 {history_days} 天）",
            payload_json=json.dumps(
                {
                    "max_workers": max_workers,
                    "history_days": history_days,
                    "mode": "smart",
                    "scopes": scopes,
                    "sync_limit": sync_limit,
                },
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        result = _task_to_read(task)
    finally:
        db.close()

    worker = threading.Thread(
        target=_run_universe_smart_sync,
        args=(task_id, max_workers, history_days, scopes, sync_limit),
        daemon=True,
    )
    worker.start()
    return result


def get_latest_universe_smart_task() -> AsyncTaskRead | None:
    db = SessionLocal()
    try:
        task = db.execute(
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.task_type == UNIVERSE_SMART_TASK_TYPE)
            .order_by(desc(AsyncTaskRecord.created_at))
            .limit(1)
        ).scalars().first()
        return _task_to_read(task) if task is not None else None
    finally:
        db.close()


def cancel_universe_smart_task(task_id: str) -> AsyncTaskRead | None:
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return None
        if task.status in ("done", "failed", "cancelled"):
            return _task_to_read(task)
        task.status = "cancelled"
        task.stage = "cancelled"
        task.message = "智能同步任务已取消"
        task.finished_at = _now()
        task.updated_at = _now()
        db.commit()
        db.refresh(task)
        return _task_to_read(task)
    finally:
        db.close()


def _run_universe_smart_sync(
    task_id: str,
    max_workers: int,
    history_days: int,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> None:
    """worker 线程主函数：按 scope 智能串联 refresh/init/backfill/incremental。"""
    if scopes is None:
        scopes = list(_ALL_UNIVERSE_SCOPES)
    SessionFactory = get_session_local()
    errors: list[dict] = []
    result_summary: dict[str, Any] = {}
    heartbeat_stop, heartbeat_thread = _start_task_heartbeat(
        SessionFactory, task_id
    )

    def _set_stage(stage: str, percent: float, message: str) -> None:
        db = SessionFactory()
        try:
            _set_task(db, task_id, stage=stage, percent=percent, message=message)
        finally:
            db.close()

    def _record_error(stage: str, exc: Exception) -> None:
        db = SessionFactory()
        try:
            _append_error(db, task_id, {"stage": stage, "error": str(exc)})
        finally:
            db.close()

    try:
        db = SessionFactory()
        try:
            _set_task(
                db, task_id,
                status="running",
                stage="smart_refresh",
                percent=0,
                message=f"开始智能同步（目标历史 {history_days} 天）",
                started_at=_now(),
            )
        finally:
            db.close()

        if _is_cancelled(task_id):
            return

        for scope in scopes:
            if _is_cancelled(task_id):
                break
            scope_label = _UNIVERSE_SCOPE_LABELS.get(scope, scope)
            stage = f"smart_refresh_{scope}"
            stage_range = _smart_stage_scope_range(scopes, scope, "refresh")
            _set_stage(stage, stage_range[0], f"智能同步：刷新{scope_label}标的列表")
            phase_ok = False
            db = SessionFactory()
            try:
                result_summary[f"{scope}_universe"] = universe_sync.refresh_universe_symbols(scope, db)
                phase_ok = True
            except Exception as exc:
                logger.exception("smart sync refresh %s failed", scope)
                errors.append({"stage": stage, "error": str(exc)})
                _record_error(stage, exc)
            finally:
                db.close()
            _set_stage(
                stage,
                stage_range[1],
                f"智能同步：{scope_label}标的列表已刷新" if phase_ok else f"智能同步：{scope_label}标的列表刷新失败，继续后续阶段",
            )

        if _is_cancelled(task_id):
            return

        for scope in scopes:
            if _is_cancelled(task_id):
                break
            scope_label = _UNIVERSE_SCOPE_LABELS.get(scope, scope)
            stage = f"smart_init_{scope}"
            stage_range = _smart_stage_scope_range(scopes, scope, "init")
            _set_stage(stage, stage_range[0], f"智能同步：初始化{scope_label}未同步K线")
            progress_cb = _make_progress_callback(
                SessionFactory,
                task_id,
                stage,
                stage_range[0],
                stage_range[1],
                f"智能同步：初始化{scope_label}未同步K线",
            )
            phase_ok = False
            try:
                result_summary[f"{scope}_init"] = universe_sync.sync_universe_bars_batch(
                    scope,
                    max_workers=max_workers,
                    history_days=history_days,
                    progress_callback=progress_cb,
                    is_cancelled=lambda: _is_cancelled(task_id),
                    sync_limit=sync_limit,
                )
                phase_ok = True
            except Exception as exc:
                logger.exception("smart sync init %s failed", scope)
                errors.append({"stage": stage, "error": str(exc)})
                _record_error(stage, exc)
            _set_stage(
                stage,
                stage_range[1],
                f"智能同步：{scope_label}初始化已完成" if phase_ok else f"智能同步：{scope_label}初始化失败，继续后续阶段",
            )

        if _is_cancelled(task_id):
            return

        for scope in scopes:
            if _is_cancelled(task_id):
                break
            scope_label = _UNIVERSE_SCOPE_LABELS.get(scope, scope)
            stage = f"smart_backfill_{scope}"
            stage_range = _smart_stage_scope_range(scopes, scope, "backfill")
            _set_stage(stage, stage_range[0], f"智能同步：回补{scope_label}历史缺口")
            progress_cb = _make_progress_callback(
                SessionFactory,
                task_id,
                stage,
                stage_range[0],
                stage_range[1],
                f"智能同步：回补{scope_label}历史缺口",
            )
            phase_ok = False
            try:
                result_summary[f"{scope}_backfill"] = universe_sync.backfill_universe_bars_batch(
                    scope,
                    max_workers=max_workers,
                    history_days=history_days,
                    progress_callback=progress_cb,
                    is_cancelled=lambda: _is_cancelled(task_id),
                    sync_limit=sync_limit,
                )
                phase_ok = True
            except Exception as exc:
                logger.exception("smart sync backfill %s failed", scope)
                errors.append({"stage": stage, "error": str(exc)})
                _record_error(stage, exc)
            _set_stage(
                stage,
                stage_range[1],
                f"智能同步：{scope_label}历史缺口已处理" if phase_ok else f"智能同步：{scope_label}历史回补失败，继续后续阶段",
            )

        if _is_cancelled(task_id):
            return

        incr_stage = "smart_incremental"
        incr_range = _SMART_PHASE_RANGES["incremental"]
        _set_stage(incr_stage, incr_range[0], "智能同步：补齐近期增量K线")

        def incremental_progress_cb(processed: int, total: int, ok: int, failed: int) -> None:
            pct = incr_range[1] if total <= 0 else incr_range[0] + (incr_range[1] - incr_range[0]) * processed / total
            db = SessionFactory()
            try:
                _set_task(
                    db,
                    task_id,
                    stage=incr_stage,
                    percent=min(pct, incr_range[1]),
                    total=total,
                    processed=processed,
                    ok_count=ok,
                    failed_count=failed,
                    message=f"智能同步：补齐近期增量K线（{processed}/{total}）",
                )
            finally:
                db.close()

        try:
            result_summary["incremental"] = universe_sync.incremental_sync(
                max_workers=max_workers,
                progress_callback=incremental_progress_cb,
                is_cancelled=lambda: _is_cancelled(task_id),
                scopes=scopes,
            )
        except Exception as exc:
            logger.exception("smart sync incremental failed")
            errors.append({"stage": incr_stage, "error": str(exc)})
            _record_error(incr_stage, exc)
            result_summary["incremental"] = {"error": str(exc)}

        incremental_result = result_summary.get("incremental", {})
        incremental_message = "智能同步：近期增量K线已补齐"
        if isinstance(incremental_result, dict) and incremental_result.get("total") == 0:
            incremental_message = "智能同步：近期增量已是最新"
        _set_stage(incr_stage, incr_range[1], incremental_message)

        db = SessionFactory()
        try:
            final_status = "cancelled" if _is_cancelled(task_id) else "done"
            final_stage = "cancelled" if final_status == "cancelled" else "done"
            incremental_result = result_summary.get("incremental", {})
            final_message = (
                f"智能同步完成：增量更新 {incremental_result.get('ok', 0)} 个，已最新 {incremental_result.get('uptodate', 0)} 个，失败 {incremental_result.get('failed', 0)} 个"
                if final_status == "done"
                else "智能同步任务已取消"
            )
            final_updates: dict[str, Any] = {
                "status": final_status,
                "stage": final_stage,
                "message": final_message,
                "result_json": json.dumps(result_summary, ensure_ascii=False, default=str),
                "finished_at": _now(),
            }
            if final_status == "done":
                final_updates["percent"] = 100
            _set_task(db, task_id, **final_updates)
        finally:
            db.close()

    except Exception:
        logger.exception("universe smart sync worker crashed: %s", task_id)
        try:
            db = SessionFactory()
            try:
                _set_task(
                    db, task_id,
                    status="failed",
                    stage="failed",
                    message="智能同步任务异常崩溃",
                    finished_at=_now(),
                )
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to mark smart sync task %s as failed", task_id)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)


# ── 历史回补任务 ─────────────────────────────────────────

# 回补阶段百分比（支持 4 个 scope，均分 0-100）

_REPAIR_STAGE_RANGES = {
    "cn-stock": (0, 45),
    "cn-etf": (45, 70),
    "us-stock": (70, 95),
    "us-etf": (95, 100),
}



def start_universe_range_repair(
    max_workers: int = 5,
    history_days: int = 365,
    chunk_days: int = 90,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> AsyncTaskRead:
    """Start a chunked range-repair task for already-synced symbols."""
    if scopes is None:
        scopes = list(_ALL_UNIVERSE_SCOPES)
    db = SessionLocal()
    try:
        existing = db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.task_type.in_((
                    UNIVERSE_SYNC_TASK_TYPE,
                    UNIVERSE_INCREMENTAL_TASK_TYPE,
                    UNIVERSE_BACKFILL_TASK_TYPE,
                    UNIVERSE_SMART_TASK_TYPE,
                    UNIVERSE_RANGE_REPAIR_TASK_TYPE,
                )),
                AsyncTaskRecord.status.in_(("queued", "running")),
            ).order_by(desc(AsyncTaskRecord.created_at))
        ).scalars().first()
        if existing is not None:
            return _task_to_read(existing)

        task_id = uuid4().hex
        scope_label = "+".join(scopes) if len(scopes) < len(_ALL_UNIVERSE_SCOPES) else "all"
        task = AsyncTaskRecord(
            id=task_id,
            task_type=UNIVERSE_RANGE_REPAIR_TASK_TYPE,
            status="queued",
            stage="queued",
            percent=0,
            message=f"Range repair queued (scope={scope_label}, days={history_days}, chunk={chunk_days})",
            payload_json=json.dumps(
                {
                    "max_workers": max_workers,
                    "history_days": history_days,
                    "chunk_days": chunk_days,
                    "mode": "range_repair",
                    "scopes": scopes,
                    "sync_limit": sync_limit,
                },
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        result = _task_to_read(task)
    finally:
        db.close()

    worker = threading.Thread(
        target=_run_universe_range_repair,
        args=(task_id, max_workers, history_days, chunk_days, scopes, sync_limit),
        daemon=True,
    )
    worker.start()
    return result



def get_latest_universe_range_repair_task() -> AsyncTaskRead | None:
    db = SessionLocal()
    try:
        task = db.execute(
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.task_type == UNIVERSE_RANGE_REPAIR_TASK_TYPE)
            .order_by(desc(AsyncTaskRecord.created_at))
            .limit(1)
        ).scalars().first()
        return _task_to_read(task) if task is not None else None
    finally:
        db.close()



def cancel_universe_range_repair_task(task_id: str) -> AsyncTaskRead | None:
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return None
        if task.status in ("done", "failed", "cancelled"):
            return _task_to_read(task)
        task.status = "cancelled"
        task.stage = "cancelled"
        task.message = "Range repair task cancelled"
        task.finished_at = _now()
        task.updated_at = _now()
        db.commit()
        db.refresh(task)
        return _task_to_read(task)
    finally:
        db.close()



def _run_universe_range_repair(
    task_id: str,
    max_workers: int,
    history_days: int,
    chunk_days: int,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> None:
    """Worker thread for chunked recent-range repair."""
    if scopes is None:
        scopes = list(_ALL_UNIVERSE_SCOPES)
    SessionFactory = get_session_local()
    errors: list[dict] = []
    result_summary: dict[str, Any] = {}

    try:
        db = SessionFactory()
        try:
            _set_task(
                db,
                task_id,
                status="running",
                stage="range_repair",
                percent=0,
                message=f"Starting range repair (days={history_days}, chunk={chunk_days})",
                started_at=_now(),
            )
        finally:
            db.close()

        if _is_cancelled(task_id):
            return

        for scope in scopes:
            if _is_cancelled(task_id):
                break

            stage_range = _REPAIR_STAGE_RANGES.get(scope, (0, 100))
            scope_label = _UNIVERSE_SCOPE_LABELS.get(scope, scope)
            stage = f"range_repair_{scope}"

            db = SessionFactory()
            try:
                _set_task(
                    db,
                    task_id,
                    stage=stage,
                    percent=stage_range[0],
                    message=f"Repairing {scope_label} recent range in chunks",
                )
            finally:
                db.close()

            progress_cb = _make_progress_callback(
                SessionFactory,
                task_id,
                stage,
                stage_range[0],
                stage_range[1],
                f"Repairing {scope_label} recent range",
            )
            try:
                sync_result = universe_sync.repair_universe_bars_batch(
                    scope,
                    max_workers=max_workers,
                    history_days=history_days,
                    chunk_days=chunk_days,
                    progress_callback=progress_cb,
                    is_cancelled=lambda: _is_cancelled(task_id),
                    sync_limit=sync_limit,
                )
                result_summary[f"{scope}_repair"] = sync_result
            except Exception as exc:
                logger.exception("range repair %s failed", scope)
                errors.append({"stage": stage, "error": str(exc)})
                db = SessionFactory()
                try:
                    _append_error(db, task_id, {"stage": stage, "error": str(exc)})
                finally:
                    db.close()

        db = SessionFactory()
        try:
            final_status = "cancelled" if _is_cancelled(task_id) else "done"
            final_stage = "cancelled" if final_status == "cancelled" else "done"
            final_message = (
                "Range repair completed" if final_status == "done"
                else "Range repair task cancelled"
            )
            final_updates: dict[str, Any] = {
                "status": final_status,
                "stage": final_stage,
                "message": final_message,
                "result_json": json.dumps(result_summary, ensure_ascii=False, default=str),
                "finished_at": _now(),
            }
            if final_status == "done":
                final_updates["percent"] = 100
            _set_task(db, task_id, **final_updates)
        finally:
            db.close()

    except Exception:
        logger.exception("universe range repair worker crashed: %s", task_id)
        try:
            db = SessionFactory()
            try:
                _set_task(
                    db,
                    task_id,
                    status="failed",
                    stage="failed",
                    message="Range repair task crashed",
                    finished_at=_now(),
                )
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to mark range repair task %s as failed", task_id)

_BACKFILL_STAGE_RANGES = {
    "cn-stock": (0, 25),
    "cn-etf": (25, 50),
    "us-stock": (50, 75),
    "us-etf": (75, 100),
}


def start_universe_backfill(
    max_workers: int = 5,
    history_days: int = 365,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> AsyncTaskRead:
    """启动历史回补任务（对已同步标的强制按新 history_days 重新拉取K线）。

    并发保护：已有 running/queued 的 universe 任何同步任务则拒绝。
    """
    if scopes is None:
        scopes = list(_ALL_UNIVERSE_SCOPES)
    db = SessionLocal()
    try:
        # 并发保护：已有运行中的 universe 任何同步任务则拒绝
        existing = db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.task_type.in_((
                    UNIVERSE_SYNC_TASK_TYPE,
                    UNIVERSE_INCREMENTAL_TASK_TYPE,
                    UNIVERSE_BACKFILL_TASK_TYPE,
                    UNIVERSE_SMART_TASK_TYPE,
                    UNIVERSE_RANGE_REPAIR_TASK_TYPE,
                )),
                AsyncTaskRecord.status.in_(("queued", "running")),
            ).order_by(desc(AsyncTaskRecord.created_at))
        ).scalars().first()
        if existing is not None:
            return _task_to_read(existing)

        task_id = uuid4().hex
        scope_label = "+".join(scopes) if len(scopes) < 4 else "全部"
        task = AsyncTaskRecord(
            id=task_id,
            task_type=UNIVERSE_BACKFILL_TASK_TYPE,
            status="queued",
            stage="queued",
            percent=0,
            message=f"历史回补任务已创建（范围：{scope_label}，历史 {history_days} 天）",
            payload_json=json.dumps(
                {
                    "max_workers": max_workers,
                    "history_days": history_days,
                    "mode": "backfill",
                    "scopes": scopes,
                    "sync_limit": sync_limit,
                },
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        result = _task_to_read(task)
    finally:
        db.close()

    worker = threading.Thread(
        target=_run_universe_backfill,
        args=(task_id, max_workers, history_days, scopes, sync_limit),
        daemon=True,
    )
    worker.start()
    return result


def get_latest_universe_backfill_task() -> AsyncTaskRead | None:
    """获取最新的历史回补任务。"""
    db = SessionLocal()
    try:
        task = db.execute(
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.task_type == UNIVERSE_BACKFILL_TASK_TYPE)
            .order_by(desc(AsyncTaskRecord.created_at))
            .limit(1)
        ).scalars().first()
        return _task_to_read(task) if task is not None else None
    finally:
        db.close()


def cancel_universe_backfill_task(task_id: str) -> AsyncTaskRead | None:
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return None
        if task.status in ("done", "failed", "cancelled"):
            return _task_to_read(task)
        task.status = "cancelled"
        task.stage = "cancelled"
        task.message = "历史回补任务已取消"
        task.finished_at = _now()
        task.updated_at = _now()
        db.commit()
        db.refresh(task)
        return _task_to_read(task)
    finally:
        db.close()


def _run_universe_backfill(
    task_id: str,
    max_workers: int,
    history_days: int,
    scopes: list[str] | None = None,
    sync_limit: int = 0,
) -> None:
    """worker 线程主函数：执行历史回补（按 scope 顺序逐个回补）。"""
    if scopes is None:
        scopes = ["cn-stock", "cn-etf", "us-stock", "us-etf"]
    SessionFactory = get_session_local()
    errors: list[dict] = []
    result_summary: dict[str, Any] = {}

    try:
        db = SessionFactory()
        try:
            _set_task(
                db, task_id,
                status="running",
                stage="backfill",
                percent=0,
                message=f"开始历史回补（{history_days} 天）",
                started_at=_now(),
            )
        finally:
            db.close()

        if _is_cancelled(task_id):
            return

        for scope in scopes:
            if _is_cancelled(task_id):
                break

            stage_range = _BACKFILL_STAGE_RANGES.get(scope, (0, 100))
            scope_label = {"cn-stock": "A股股票", "cn-etf": "A股ETF", "us-stock": "美股股票", "us-etf": "美股ETF"}.get(scope, scope)

            db = SessionFactory()
            try:
                _set_task(db, task_id, stage=f"backfill_{scope}", percent=stage_range[0],
                          message=f"开始回补 {scope_label} 历史K线")
            finally:
                db.close()

            progress_cb = _make_progress_callback(
                SessionFactory, task_id, f"backfill_{scope}",
                stage_range[0], stage_range[1], f"回补{scope_label}历史K线",
            )
            try:
                sync_result = universe_sync.backfill_universe_bars_batch(
                    scope, max_workers=max_workers, history_days=history_days,
                    progress_callback=progress_cb, is_cancelled=lambda: _is_cancelled(task_id),
                    sync_limit=sync_limit,
                )
                result_summary[f"{scope}_backfill"] = sync_result
            except Exception as exc:
                logger.exception("backfill %s bars failed", scope)
                errors.append({"stage": f"backfill_{scope}", "error": str(exc)})
                db = SessionFactory()
                try:
                    _append_error(db, task_id, {"stage": f"backfill_{scope}", "error": str(exc)})
                finally:
                    db.close()

        # 完成
        db = SessionFactory()
        try:
            final_status = "cancelled" if _is_cancelled(task_id) else "done"
            final_stage = "cancelled" if final_status == "cancelled" else "done"
            final_message = (
                "历史回补完成" if final_status == "done"
                else "历史回补任务已取消"
            )
            final_updates: dict[str, Any] = {
                "status": final_status,
                "stage": final_stage,
                "message": final_message,
                "result_json": json.dumps(result_summary, ensure_ascii=False, default=str),
                "finished_at": _now(),
            }
            if final_status == "done":
                final_updates["percent"] = 100
            _set_task(db, task_id, **final_updates)
        finally:
            db.close()

    except Exception:
        logger.exception("universe backfill worker crashed: %s", task_id)
        try:
            db = SessionFactory()
            try:
                _set_task(
                    db, task_id,
                    status="failed",
                    stage="failed",
                    message="历史回补任务异常崩溃",
                    finished_at=_now(),
                )
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to mark backfill task %s as failed", task_id)
