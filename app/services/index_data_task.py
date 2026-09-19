"""市场指数日线同步异步任务（P3+：Benchmark 对比曲线基础设施）。

调度入口：scheduled_tasks 每日 16:05 触发（在组合净值快照 16:00 之后）
手动触发：通过 scheduled_tasks API 或一次性回补历史数据

与 portfolio_equity_snapshot 任务模式保持一致：
- create_index_daily_sync_task: 去重 + 启动 worker
- _run_index_sync_task: 实际执行，带进度跟踪与终态保护
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

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
from app.services.index_data import sync_index_daily


logger = logging.getLogger(__name__)
TASK_TYPE = "index_daily_sync"


def create_index_daily_sync_task(*, symbol: str = "000300", lookback_days: int = 5):
    """调度入口：去重后启动后台 worker。

    Args:
        symbol: 指数代码，如 "000300"
        lookback_days: 回补天数（默认 5 天，覆盖节假日缺口）
    """
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {"queued", "running"}:
        return existing[0]
    task = create_async_task(
        TASK_TYPE,
        {"symbol": symbol, "lookback_days": lookback_days},
    )
    _start_worker(task.id, _run_index_sync_task)
    return task


def _run_index_sync_task(task_id: str) -> None:
    """worker：执行指数日线同步，带进度跟踪与终态保护。"""
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return

        # 解析 payload
        payload = json.loads(task.payload_json or "{}")
        symbol = str(payload.get("symbol", "000300"))
        lookback_days = int(payload.get("lookback_days", 5))

        _set_task(
            db,
            task_id,
            status="running",
            stage="fetch",
            percent=10,
            message=f"Fetching index {symbol} daily bars (lookback {lookback_days}d)",
            started_at=_now(),
        )

        end_date = date.today()
        start_date = end_date - timedelta(days=lookback_days)

        try:
            result = sync_index_daily(
                db, symbol,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:
            _set_task(
                db,
                task_id,
                status="failed",
                stage="failed",
                percent=100,
                message=f"Index sync failed: {exc}",
                total=0,
                processed=0,
                ok_count=0,
                failed_count=1,
                finished_at=_now(),
            )
            _append_error(task, {"scope": "index_daily_sync", "error": str(exc)})
            db.commit()
            return

        # 终态检查（防止取消信号被覆盖）
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return

        date_range_start, date_range_end = result.date_range
        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            total=result.received,
            processed=result.written,
            ok_count=result.written,
            failed_count=result.skipped,
            message=f"Index {symbol} sync: {result.written} written, {result.skipped} skipped",
            result_json=json.dumps(
                {
                    "symbol": result.symbol,
                    "received": result.received,
                    "written": result.written,
                    "skipped": result.skipped,
                    "date_range_start": date_range_start.isoformat() if date_range_start else None,
                    "date_range_end": date_range_end.isoformat() if date_range_end else None,
                },
                ensure_ascii=False,
                default=str,
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Index daily sync task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            _append_error(task, {"scope": "index_daily_sync", "error": str(exc)})
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


__all__ = [
    "TASK_TYPE",
    "create_index_daily_sync_task",
]
