"""Asynchronous bounded financial-report synchronization."""
from __future__ import annotations

import json
import logging

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
    sync_symbol_financial_reports,
)


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
        for index, symbol in enumerate(symbols, start=1):
            task = db.get(AsyncTaskRecord, task_id)
            if task is None or task.status == "cancelled":
                return
            try:
                count = sync_symbol_financial_reports(db, symbol)
                db.commit()
                if count:
                    success += 1
                    records += count
                else:
                    skipped += 1
            except Exception as exc:
                db.rollback()
                failed += 1
                if len(errors) < 20:
                    errors.append(
                        {"symbol": symbol.symbol, "error": str(exc)}
                    )
                logger.warning(
                    "Financial report task failed for %s: %s",
                    symbol.symbol,
                    exc,
                )
            _set_task(
                db,
                task_id,
                stage="fetch",
                percent=round(index / max(total, 1) * 95, 1),
                total=total,
                processed=index,
                ok_count=success,
                failed_count=failed,
                current_item=symbol.symbol,
                message=f"Financial reports {index}/{total}",
                errors_json=json.dumps(errors, ensure_ascii=False),
            )
        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            total=total,
            processed=total,
            ok_count=success,
            failed_count=failed,
            current_item=None,
            message="Financial report synchronization completed",
            result_json=json.dumps(
                {
                    "source": source,
                    "limit": limit,
                    "total": total,
                    "success": success,
                    "skipped": skipped,
                    "failed": failed,
                    "records": records,
                },
                ensure_ascii=False,
            ),
            errors_json=json.dumps(errors, ensure_ascii=False),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Financial report task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


__all__ = ["TASK_TYPE", "create_financial_report_task"]
