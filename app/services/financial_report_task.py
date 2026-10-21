"""Asynchronous bounded financial-report synchronization."""
from __future__ import annotations

import json
import logging
from datetime import date

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.services.async_tasks import (
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    list_async_tasks,
)
from app.services.financial_data import (
    resolve_financial_report_symbols,
    sync_financial_announcements,
)
from app.services.sync_progress import CancellationProbe, ProgressCheckpoint


logger = logging.getLogger(__name__)
TASK_TYPE = "financial_report_sync"


def create_financial_report_task(
    *, source: str = "watchlist", limit: int = 20
):
    if source not in {"watchlist", "positions", "all"}:
        raise ValueError("unsupported financial report source")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {"queued", "running"}:
        return existing[0]
    task = create_async_task(
        TASK_TYPE, {"source": source, "limit": limit}
    )
    _start_worker(task.id, _run_financial_report_task)
    return task


def _run_financial_report_task(task_id: str) -> None:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        payload = json.loads(task.payload_json or "{}")
        source = str(payload.get("source") or "watchlist")
        limit = int(payload.get("limit") or 20)
        symbols = resolve_financial_report_symbols(
            db, source=source, limit=limit
        )
        total = len(symbols)
        _set_task(
            db,
            task_id,
            status="running",
            stage="fetch",
            percent=5,
            total=total,
            processed=0,
            message="Starting financial report synchronization",
            started_at=_now(),
        )
        success = 0
        skipped = 0
        failed = 0
        records = 0
        errors: list[dict[str, str]] = []
        def should_cancel():
            with SessionLocal() as reader:
                active = reader.get(AsyncTaskRecord, task_id)
                return active is None or active.status == "cancelled" or bool(active.cancel_requested)

        def on_result(symbol, count, summary):
            db.commit()
            processed = summary["success"] + summary["skipped"]
            _set_task(db, task_id, stage="fetch", percent=round(processed / max(total, 1) * 95, 1),
                      total=total, processed=processed, ok_count=summary["success"], failed_count=0,
                      current_item=symbol.symbol, message=f"Financial reports {processed}/{total}")

        checkpoint = ProgressCheckpoint(on_result)
        try:
            summary = sync_financial_announcements(db, symbols, date.today(), date.today(),
                should_cancel=CancellationProbe(should_cancel), on_result=checkpoint)
        finally:
            checkpoint.flush()
        if should_cancel():
            return
        success, skipped, failed, records = (summary[key] for key in ("success", "skipped", "failed", "records"))
        from app.services.external_data_sync_task import finalize_legacy_external_sync
        written_dates = [date.fromisoformat(summary[key]) for key in ("written_start_date", "written_end_date") if summary.get(key)]
        publication = finalize_legacy_external_sync(db, task_id, "financial", records, written_dates)
        failed += int(publication["warehouse_mirror"]["status"] == "warning")
        _set_task(
            db,
            task_id,
            status="failed" if failed else "done",
            stage="failed" if failed else "done",
            percent=100,
            total=total,
            processed=total,
            ok_count=success,
            failed_count=failed,
            current_item=None,
            message=("财报同步存在失败，请查看错误" if failed else "所选公告日期没有新增或变更财报" if not records else "Financial report synchronization completed"),
            result_json=json.dumps(
                {
                    "source": source,
                    "limit": limit,
                    "total": total,
                    "success": success,
                    "skipped": skipped,
                    "failed": failed,
                    "records": records,
                    **publication,
                },
                ensure_ascii=False,
                default=str,
            ),
            errors_json=json.dumps(errors, ensure_ascii=False),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Financial report task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None and task.cancel_requested:
            task.status = task.stage = "cancelled"
            task.finished_at = task.updated_at = _now()
            db.commit()
            return
        if task is not None and task.status != "cancelled":
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


__all__ = ["TASK_TYPE", "create_financial_report_task"]
