"""Observable asynchronous tasks for external-data synchronization."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4

from sqlalchemy import desc, distinct, func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.async_task import AsyncTaskRecord
from app.models.capital_flow import CapitalFlow, NorthboundFlow
from app.models.etf_indicator import EtfIndicator
from app.models.financial_report import StockFinancialReport
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.schemas.async_task import AsyncTaskRead
from app.services.async_tasks import _task_to_read, get_async_task

logger = logging.getLogger(__name__)

ExternalDataset = Literal[
    "fundamental",
    "financial",
    "lhb",
    "hot_rank",
    "tail_proxy",
    "capital_flow",
    "etf",
]

EXTERNAL_DATASETS: tuple[ExternalDataset, ...] = (
    "fundamental",
    "financial",
    "lhb",
    "hot_rank",
    "tail_proxy",
    "capital_flow",
    "etf",
)

_TASK_TYPES = {dataset: f"external_sync_{dataset}" for dataset in EXTERNAL_DATASETS}
_DATASET_BY_TASK_TYPE = {value: key for key, value in _TASK_TYPES.items()}
_SYNC_THROTTLE_SECONDS = 0.3


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_external_symbols(
    db: Session,
    source: Literal["watchlist", "positions", "all"],
    asset_type: str | None,
) -> list[Symbol]:
    """Resolve the symbol scope shared by legacy and asynchronous endpoints."""
    if source == "all":
        stmt = select(Symbol).where(Symbol.is_active == 1)
    elif source == "watchlist":
        from app.models.watchlist import WatchlistItem

        stmt = (
            select(Symbol)
            .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1)
        )
    elif source == "positions":
        from app.models.portfolio import Position

        stmt = (
            select(Symbol)
            .join(Position, Position.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1)
        )
    else:
        return []
    if asset_type:
        stmt = stmt.where(Symbol.asset_type == asset_type)
    return list(db.execute(stmt).scalars().all())


def _update_task(db: Session, task_id: str, **updates) -> AsyncTaskRecord | None:
    task = db.get(AsyncTaskRecord, task_id)
    if task is None:
        return None
    if task.status in ("done", "failed", "cancelled"):
        return task
    for key, value in updates.items():
        setattr(task, key, value)
    task.updated_at = _now()
    db.commit()
    db.refresh(task)
    return task


def _is_cancelled(db: Session, task_id: str) -> bool:
    task = db.get(AsyncTaskRecord, task_id)
    return task is None or task.status == "cancelled"


def _progress_message(dataset: ExternalDataset, processed: int, total: int) -> str:
    return f"{dataset} sync in progress ({processed}/{total})"


def _run_symbol_sync(
    db: Session,
    task_id: str,
    dataset: Literal["fundamental", "financial", "capital_flow", "etf"],
    payload: dict,
) -> dict | None:
    source = payload.get("source", "watchlist")
    asset_type = "etf" if dataset == "etf" else "stock"
    symbols = resolve_external_symbols(db, source, asset_type)
    result = {
        "dataset": dataset,
        "total": len(symbols),
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "records": 0,
        "errors": [],
    }
    _update_task(
        db,
        task_id,
        status="running",
        stage="prepare",
        percent=2,
        total=len(symbols),
        message=f"Resolved {len(symbols)} symbols",
        started_at=_now(),
    )

    if dataset == "capital_flow" and payload.get("include_northbound", True):
        from app.services.capital_flow_data import sync_northbound_flow

        _update_task(db, task_id, stage="northbound", percent=4, message="Syncing northbound flow")
        try:
            sync_northbound_flow(db, days=30)
            db.commit()
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"northbound: {exc}")
            logger.warning("northbound sync failed in external task: %s", exc)

    today = date.today()
    for index, symbol in enumerate(symbols):
        if _is_cancelled(db, task_id):
            return None
        try:
            if dataset == "fundamental":
                from app.services.fundamental_data import sync_symbol_valuation

                item = sync_symbol_valuation(db, symbol, today)
                count = 1 if item is not None else 0
            elif dataset == "financial":
                from app.services.financial_data import sync_symbol_financial_reports

                count = sync_symbol_financial_reports(db, symbol)
            elif dataset == "capital_flow":
                from app.services.capital_flow_data import sync_symbol_capital_flow

                item = sync_symbol_capital_flow(db, symbol, today)
                count = 1 if item is not None else 0
            else:
                from app.services.etf_basic_data import sync_etf_indicator

                item = sync_etf_indicator(db, symbol, today)
                count = 1 if item is not None else 0

            if count > 0:
                result["success"] += 1
                result["records"] += count
            else:
                result["skipped"] += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            result["failed"] += 1
            if len(result["errors"]) < 20:
                result["errors"].append(f"{symbol.symbol}: {exc}")
            logger.warning("%s sync failed for %s: %s", dataset, symbol.symbol, exc)

        processed = index + 1
        percent = 100 if not symbols else min(99, 5 + 94 * processed / len(symbols))
        _update_task(
            db,
            task_id,
            status="running",
            stage="sync",
            percent=percent,
            total=len(symbols),
            processed=processed,
            ok_count=result["success"],
            failed_count=result["failed"],
            current_item=symbol.symbol,
            message=_progress_message(dataset, processed, len(symbols)),
        )
        if index < len(symbols) - 1:
            time.sleep(_SYNC_THROTTLE_SECONDS)
    return result


def _run_bulk_sync(
    db: Session,
    task_id: str,
    dataset: Literal["lhb", "hot_rank", "tail_proxy"],
    payload: dict,
) -> dict:
    _update_task(
        db,
        task_id,
        status="running",
        stage="fetch",
        percent=10,
        message=f"Fetching {dataset} data from source",
        started_at=_now(),
    )
    if dataset == "lhb":
        from datetime import timedelta

        from app.services.lhb_data import sync_lhb_institution_trades

        lookback_days = int(payload.get("lookback_days", 30))
        end_date = date.today()
        summary = sync_lhb_institution_trades(
            db,
            start_date=end_date - timedelta(days=lookback_days - 1),
            end_date=end_date,
        )
        result = {
            "dataset": dataset,
            "total": summary.received,
            "success": summary.written,
            "skipped": summary.unmatched,
            "failed": 0,
            "records": summary.written,
            "errors": [],
        }
    elif dataset == "hot_rank":
        from app.services.hot_rank_data import sync_hot_rank_snapshot

        summary = sync_hot_rank_snapshot(db)
        result = {
            "dataset": dataset,
            "total": summary.received,
            "success": summary.written,
            "skipped": summary.unmatched,
            "failed": 0,
            "records": summary.written,
            "errors": [],
        }
    else:
        from app.services.tail_proxy_data import sync_tail_proxy_snapshots

        summary = sync_tail_proxy_snapshots(
            db,
            source="candidates",
            limit=int(payload.get("limit", 20)),
        )
        result = {
            "dataset": dataset,
            "total": summary.total,
            "success": summary.written,
            "skipped": summary.skipped,
            "failed": summary.failed,
            "records": summary.written,
            "errors": list(summary.errors),
        }
    db.commit()
    return result


def _run_external_sync_task(task_id: str, dataset: ExternalDataset, payload: dict) -> None:
    db = SessionLocal()
    try:
        if dataset in ("fundamental", "financial", "capital_flow", "etf"):
            result = _run_symbol_sync(db, task_id, dataset, payload)
        else:
            result = _run_bulk_sync(db, task_id, dataset, payload)
        if result is None or _is_cancelled(db, task_id):
            return
        processed = result["success"] + result["skipped"] + result["failed"]
        _update_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            total=result["total"],
            processed=processed,
            ok_count=result["success"],
            failed_count=result["failed"],
            current_item=None,
            message=(
                f"Sync completed: success {result['success']}, "
                f"skipped {result['skipped']}, failed {result['failed']}"
            ),
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            errors_json=json.dumps(
                [{"stage": "sync", "error": error} for error in result["errors"][-20:]],
                ensure_ascii=False,
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("external data task %s failed", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None and task.status not in ("done", "failed", "cancelled"):
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.errors_json = json.dumps(
                [{"stage": task.stage, "error": str(exc)}],
                ensure_ascii=False,
            )
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


def start_external_data_sync(dataset: ExternalDataset, payload: dict) -> AsyncTaskRead:
    if dataset not in EXTERNAL_DATASETS:
        raise ValueError(f"Unsupported external dataset: {dataset}")
    db = SessionLocal()
    try:
        existing = db.execute(
            select(AsyncTaskRecord)
            .where(
                AsyncTaskRecord.task_type.like("external_sync_%"),
                AsyncTaskRecord.status.in_(("queued", "running")),
            )
            .order_by(desc(AsyncTaskRecord.created_at))
        ).scalars().first()
        if existing is not None:
            raise RuntimeError("Another external-data sync task is already running")

        task_id = uuid4().hex
        task = AsyncTaskRecord(
            id=task_id,
            task_type=_TASK_TYPES[dataset],
            status="queued",
            stage="queued",
            percent=0,
            message="External-data sync task created",
            payload_json=json.dumps({"dataset": dataset, **payload}, ensure_ascii=False),
        )
        db.add(task)
        db.commit()
    finally:
        db.close()

    worker = threading.Thread(
        target=_run_external_sync_task,
        args=(task_id, dataset, payload),
        name=f"external-sync-{dataset}-{task_id[:8]}",
        daemon=True,
    )
    worker.start()
    created = get_async_task(task_id)
    if created is None:
        raise RuntimeError("External-data sync task was not created")
    return created


def get_external_sync_task(task_id: str) -> AsyncTaskRead | None:
    task = get_async_task(task_id)
    if task is None or task.task_type not in _DATASET_BY_TASK_TYPE:
        return None
    return task


def _table_stats(db: Session, model, symbol_column, date_column) -> dict:
    records, symbols, latest_date, last_updated_at = db.execute(
        select(
            func.count(model.id),
            func.count(distinct(symbol_column)),
            func.max(date_column),
            func.max(model.created_at),
        )
    ).one()
    return {
        "records": int(records or 0),
        "symbols": int(symbols or 0),
        "latest_date": latest_date,
        "last_updated_at": last_updated_at,
    }


def get_external_data_overview(db: Session) -> dict:
    stats = {
        "fundamental": _table_stats(db, StockValuation, StockValuation.symbol_id, StockValuation.trade_date),
        "financial": _table_stats(
            db,
            StockFinancialReport,
            StockFinancialReport.symbol_id,
            StockFinancialReport.announcement_date,
        ),
        "lhb": _table_stats(db, LhbInstitutionTrade, LhbInstitutionTrade.symbol, LhbInstitutionTrade.trade_date),
        "hot_rank": _table_stats(
            db,
            StockHotRankSnapshot,
            StockHotRankSnapshot.symbol,
            StockHotRankSnapshot.trade_date,
        ),
        "tail_proxy": _table_stats(
            db,
            TailAccumulationSnapshot,
            TailAccumulationSnapshot.symbol,
            TailAccumulationSnapshot.trade_date,
        ),
        "capital_flow": _table_stats(db, CapitalFlow, CapitalFlow.symbol_id, CapitalFlow.trade_date),
        "etf": _table_stats(db, EtfIndicator, EtfIndicator.symbol_id, EtfIndicator.trade_date),
    }
    northbound_records, northbound_latest, northbound_updated = db.execute(
        select(
            func.count(NorthboundFlow.id),
            func.max(NorthboundFlow.trade_date),
            func.max(NorthboundFlow.created_at),
        )
    ).one()
    stats["capital_flow"]["records"] += int(northbound_records or 0)
    if northbound_latest and (
        stats["capital_flow"]["latest_date"] is None
        or northbound_latest > stats["capital_flow"]["latest_date"]
    ):
        stats["capital_flow"]["latest_date"] = northbound_latest
    if northbound_updated and (
        stats["capital_flow"]["last_updated_at"] is None
        or northbound_updated > stats["capital_flow"]["last_updated_at"]
    ):
        stats["capital_flow"]["last_updated_at"] = northbound_updated

    task_rows = db.execute(
        select(AsyncTaskRecord)
        .where(AsyncTaskRecord.task_type.in_(tuple(_DATASET_BY_TASK_TYPE)))
        .order_by(desc(AsyncTaskRecord.created_at))
    ).scalars().all()
    latest_tasks: dict[str, AsyncTaskRecord] = {}
    for task in task_rows:
        dataset = _DATASET_BY_TASK_TYPE[task.task_type]
        latest_tasks.setdefault(dataset, task)

    datasets = []
    for dataset in EXTERNAL_DATASETS:
        latest_task = latest_tasks.get(dataset)
        datasets.append(
            {
                "dataset": dataset,
                **stats[dataset],
                "latest_task": _task_to_read(latest_task).model_dump() if latest_task else None,
            }
        )
    return {
        "datasets": datasets,
        "total_records": sum(item["records"] for item in datasets),
        "covered_symbols": sum(item["symbols"] for item in datasets),
        "available_datasets": sum(item["latest_date"] is not None for item in datasets),
        "running_tasks": sum(
            bool(item["latest_task"] and item["latest_task"]["status"] in ("queued", "running"))
            for item in datasets
        ),
        "refreshed_at": _now(),
    }
