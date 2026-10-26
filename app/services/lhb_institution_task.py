"""Asynchronous market-wide institution-seat LHB synchronization."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.services.async_tasks import (
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    list_async_tasks,
)
from app.services.lhb_data import sync_lhb_institution_trades, LhbInstitutionSyncResult


logger = logging.getLogger(__name__)
TASK_TYPE = "lhb_institution_sync"


def create_lhb_institution_task(*, lookback_days: int = 3):
    if not 1 <= lookback_days <= 31:
        raise ValueError("lookback_days must be between 1 and 31")
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {"queued", "running"}:
        return existing[0]
    task = create_async_task(
        TASK_TYPE, {"lookback_days": lookback_days}
    )
    _start_worker(task.id, _run_lhb_institution_task)
    return task


def _run_lhb_institution_task(task_id: str) -> None:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        payload = json.loads(task.payload_json or "{}")
        lookback_days = int(payload.get("lookback_days") or 3)
        end_date = date.today()
        start_date = end_date - timedelta(days=lookback_days - 1)
        _set_task(
            db,
            task_id,
            status="running",
            stage="fetch",
            percent=10,
            message="Fetching institution-seat LHB data",
            started_at=_now(),
        )
        from app.services.market_session import is_cn_trading_day
        summary = (sync_lhb_institution_trades(db, start_date=start_date, end_date=end_date)
                   if is_cn_trading_day(db, end_date) else
                   LhbInstitutionSyncResult(start_date, end_date, 0, 0, 0, skip_reason="market_closed"))
        db.commit()
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        from app.services.external_data_sync_task import finalize_legacy_external_sync
        publication = finalize_legacy_external_sync(db, task_id, "lhb", summary.written,
                                                  summary.written_dates, skip_reason=summary.skip_reason)
        # Provider rows are already durable in SQL. A warehouse mirror warning
        # must not turn a successful collection into a failed task; the warning
        # stays in result_json so the mirror retry/patrol can pick it up.
        mirror_warning = publication["warehouse_mirror"]["status"] == "warning"
        if mirror_warning:
            message = "龙虎榜已入库，因子仓库镜像待重试"
        elif summary.skip_reason == "market_closed":
            message = "A 股休市，已跳过龙虎榜采集"
        else:
            message = "Institution-seat LHB synchronization completed"
        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            total=summary.received,
            processed=summary.received,
            ok_count=summary.written,
            failed_count=0,
            message=message,
            result_json=json.dumps(
                {
                    "start_date": summary.start_date,
                    "end_date": summary.end_date,
                    "received": summary.received,
                    "written": summary.written,
                    "unmatched": summary.unmatched,
                    "skip_reason": summary.skip_reason,
                    **publication,
                },
                ensure_ascii=False,
                default=str,
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("LHB institution task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None and task.status != "cancelled":
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


__all__ = ["TASK_TYPE", "create_lhb_institution_task"]
