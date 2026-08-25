"""Observable asynchronous tasks for external-data synchronization."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import and_, desc, distinct, func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.async_task import AsyncTaskRecord
from app.models.capital_flow import CapitalFlow, NorthboundFlow
from app.models.daily_bar import DailyBar
from app.models.data_sync_plan import DataSyncPartition, DataSyncPlan
from app.models.etf_indicator import EtfIndicator
from app.models.financial_report import StockFinancialReport
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.models.universe import UniverseSymbol
from app.schemas.async_task import AsyncTaskRead
from app.services.async_tasks import _task_to_read, cancel_async_task, get_async_task

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
# Provider-level rate limiting is handled by akshare_registry. Keep only a
# small cooperative yield between per-symbol historical requests; the daily
# snapshot path does a single provider call and does not use this delay.
_SYNC_THROTTLE_SECONDS = 0.05
_MARKET_PRIORITY_TASK_TYPES = (
    "market_data_sync",
    "history_initialization",
    "universe_sync",
    "universe_incremental_sync",
    "universe_backfill",
    "universe_smart_sync",
    "universe_range_repair",
)
_MARKET_PRIORITY_WAIT_SECONDS = 5.0

ExternalSyncMode = Literal["incremental", "backfill"]

_DATASET_CAPABILITIES: dict[ExternalDataset, dict[str, Any]] = {
    "fundamental": {"modes": {"incremental", "backfill"}, "history_limit_days": None, "reason": "通过个股历史估值接口写入真实交易日 PE/PB 快照；接口未返回的日期不会填补。"},
    "financial": {"modes": {"incremental", "backfill"}, "history_limit_days": None, "reason": "按公告日保存可取得的报告历史。"},
    "capital_flow": {"modes": {"incremental", "backfill"}, "history_limit_days": 100, "reason": "提供商通常仅返回近 100 个交易日前后数据；超出范围必须接入新数据源或导入。"},
    "lhb": {"modes": {"incremental", "backfill"}, "history_limit_days": 31, "reason": "龙虎榜为事件数据，单次区间受接口限制。"},
    "hot_rank": {"modes": {"incremental"}, "history_limit_days": 0, "reason": "人气榜接口仅提供当前快照。"},
    "tail_proxy": {"modes": {"incremental"}, "history_limit_days": 0, "reason": "尾盘代理当前仅做候选池当天采集。"},
    "etf": {"modes": {"incremental"}, "history_limit_days": 0, "reason": "ETF 指标当前仅支持当日快照。"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_external_symbols(
    db: Session,
    source: Literal["watchlist", "positions", "all"],
    asset_type: str | None,
    watchlist_id: int | None = None,
) -> list[Symbol]:
    """Resolve the symbol scope shared by legacy and asynchronous endpoints."""
    if source == "all":
        # ``symbols`` only contains business/promoted symbols, whereas the
        # market-data base owns the complete, already-synced market universe.
        # External factor inputs reference Symbol.id, so materialise the
        # missing business records here without copying any daily-bar data.
        # This makes the UI's "all" scope genuinely mean all usable market
        # symbols instead of the small promoted/watchlist subset.
        universe_stmt = select(UniverseSymbol).where(UniverseSymbol.is_synced == 1)
        if asset_type:
            universe_stmt = universe_stmt.where(UniverseSymbol.asset_type == asset_type)
        universe_rows = db.execute(universe_stmt.order_by(UniverseSymbol.id)).scalars().all()

        universe_codes = [row.symbol for row in universe_rows]
        existing_by_code = {
            item.symbol: item
            for item in db.execute(
                select(Symbol).where(Symbol.symbol.in_(universe_codes))
            ).scalars().all()
        } if universe_codes else {}
        for row in universe_rows:
            item = existing_by_code.get(row.symbol)
            if item is None:
                item = Symbol(
                    symbol=row.symbol,
                    name=row.name or row.symbol,
                    asset_type=row.asset_type,
                    market=row.market,
                    board=row.board,
                    industry=row.industry,
                    listed_at=row.listed_at,
                    is_active=1,
                )
                db.add(item)
                existing_by_code[row.symbol] = item
            elif item.is_active != 1:
                item.is_active = 1
        db.flush()

        # Keep active legacy symbols which do not yet have a universe record
        # (for example manually maintained instruments) in the all scope.
        stmt = select(Symbol).where(Symbol.is_active == 1)
    elif source == "watchlist":
        from app.models.watchlist import WatchlistItem

        stmt = (
            select(Symbol).distinct()
            .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
            .where(
                Symbol.is_active == 1,
                WatchlistItem.status.in_(("watching", "ready")),
            )
        )
        if watchlist_id is not None:
            stmt = stmt.where(WatchlistItem.watchlist_id == watchlist_id)
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
    return sorted(db.execute(stmt).scalars().all(), key=lambda symbol: symbol.id)


def _update_task(db: Session, task_id: str, **updates) -> AsyncTaskRecord | None:
    task = db.get(AsyncTaskRecord, task_id)
    if task is None:
        return None
    if task.status in ("done", "failed", "cancelled"):
        return task
    # cancel_async_task marks a running task with a cancellation token first;
    # do not let a late worker checkpoint move it forward while it is stopping.
    if int(getattr(task, "cancel_requested", 0) or 0) == 1 and updates.get("status") != "cancelled":
        return task
    for key, value in updates.items():
        setattr(task, key, value)
    if task.status in ("done", "failed", "cancelled"):
        task.is_terminal_locked = 1
        if task.finished_at is None:
            task.finished_at = _now()
    task.updated_at = _now()
    if task.status in ("queued", "running"):
        task.heartbeat_at = _now()
        task.last_progress_at = task.last_progress_at or _now()
    db.commit()
    db.refresh(task)
    return task


def _is_cancelled(db: Session, task_id: str) -> bool:
    task = db.get(AsyncTaskRecord, task_id)
    return task is None or task.status == "cancelled" or int(getattr(task, "cancel_requested", 0) or 0) == 1


def _mark_cancelled(db: Session, task_id: str, message: str = "Sync cancelled by user") -> None:
    """Persist the cancellation request as a terminal task state."""
    task = db.get(AsyncTaskRecord, task_id)
    if task is None or task.status in ("done", "failed", "cancelled"):
        return
    task.status = "cancelled"
    task.stage = "cancelled"
    task.message = message
    task.finished_at = _now()
    task.updated_at = _now()
    task.is_terminal_locked = 1
    db.commit()


def _active_market_priority_task(db: Session) -> AsyncTaskRecord | None:
    """Read-only market-data priority gate; universe implementation stays untouched."""
    return db.execute(
        select(AsyncTaskRecord)
        .where(
            AsyncTaskRecord.task_type.in_(_MARKET_PRIORITY_TASK_TYPES),
            AsyncTaskRecord.status.in_(("queued", "running")),
        )
        .order_by(desc(AsyncTaskRecord.created_at))
    ).scalars().first()


def _wait_for_market_priority(db: Session, task_id: str) -> bool:
    """Yield external provider capacity when a base-market task becomes active."""
    while True:
        if _is_cancelled(db, task_id):
            return False
        market_task = _active_market_priority_task(db)
        if market_task is None:
            return True
        _update_task(
            db,
            task_id,
            status="running",
            stage="waiting_market",
            message=(
                "Paused for market-data priority task "
                f"{market_task.task_type} ({market_task.id[:8]})"
            ),
        )
        time.sleep(_MARKET_PRIORITY_WAIT_SECONDS)


def _progress_message(dataset: ExternalDataset, processed: int, total: int) -> str:
    return f"{dataset} sync in progress ({processed}/{total})"


def _as_date(value: object | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise ValueError("invalid date value")


def build_external_sync_plan(
    dataset: ExternalDataset,
    payload: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Build a truthful sync plan before making any remote request."""
    if dataset not in EXTERNAL_DATASETS:
        raise ValueError(f"Unsupported external dataset: {dataset}")
    capability = _DATASET_CAPABILITIES[dataset]
    mode = str(payload.get("mode", "incremental"))
    if mode not in capability["modes"]:
        raise ValueError(f"{dataset} does not support {mode}: {capability['reason']}")
    reference_date = today or date.today()
    requested_end = _as_date(payload.get("end_date")) or reference_date
    requested_start = _as_date(payload.get("start_date"))
    lookback_days = int(payload.get("lookback_days", 1 if mode == "incremental" else 60))
    if lookback_days < 1:
        raise ValueError("lookback_days must be at least 1")
    if requested_start is None:
        requested_start = requested_end - timedelta(days=lookback_days - 1)
    if requested_start > requested_end:
        raise ValueError("start_date must be on or before end_date")
    if mode == "incremental":
        requested_start = requested_end
    span_days = (requested_end - requested_start).days + 1
    history_limit_days = capability["history_limit_days"]
    # A limit of zero denotes a current-snapshot-only provider. The one-day
    # incremental request is valid; only multi-day ranges should be rejected.
    if history_limit_days is not None and history_limit_days > 0 and span_days > history_limit_days:
        raise ValueError(
            f"{dataset} requested {span_days} days exceeds provider limit {history_limit_days}: {capability['reason']}"
        )
    return {
        "dataset": dataset,
        "mode": mode,
        "requested_start_date": requested_start.isoformat(),
        "requested_end_date": requested_end.isoformat(),
        "requested_span_days": span_days,
        "provider_history_limit_days": history_limit_days,
        "provider_reason": capability["reason"],
        "partition_strategy": "symbol_batches" if dataset in {"financial", "capital_flow", "fundamental", "etf"} else "single_fetch",
        "symbol_batch_size": min(max(int(payload.get("limit", 20)), 1), 50),
    }


def get_external_sync_capabilities() -> dict[str, dict[str, Any]]:
    return {
        dataset: {
            "modes": sorted(values["modes"]),
            "history_limit_days": values["history_limit_days"],
            "reason": values["reason"],
        }
        for dataset, values in _DATASET_CAPABILITIES.items()
    }


_SYMBOL_PARTITION_DATASETS = {"fundamental", "financial", "capital_flow", "etf"}
_GAP_REPAIR_DATASETS = {"fundamental", "financial", "capital_flow"}


def _create_persisted_sync_plan(
    db: Session,
    *,
    task_id: str,
    dataset: ExternalDataset,
    payload: dict[str, Any],
    frozen_plan: dict[str, Any],
    parent_plan_id: str | None = None,
    partition_specs: list[dict[str, Any]] | None = None,
) -> DataSyncPlan:
    start_date = _as_date(frozen_plan.get("requested_start_date"))
    end_date = _as_date(frozen_plan.get("requested_end_date"))
    if start_date is None or end_date is None:
        raise ValueError("sync plan date range is invalid")
    if partition_specs is None:
        if dataset in _SYMBOL_PARTITION_DATASETS:
            asset_type = "etf" if dataset == "etf" else "stock"
            symbols = resolve_external_symbols(
                db, payload.get("source", "watchlist"), asset_type, payload.get("watchlist_id")
            )
            partition_specs = [
                {
                    "partition_key": f"symbol:{symbol.id}:{start_date}:{end_date}",
                    "symbol_id": symbol.id,
                    "symbol": symbol.symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                }
                for symbol in symbols
            ]
        else:
            partition_specs = [
                {
                    "partition_key": f"dataset:{dataset}:{start_date}:{end_date}",
                    "symbol_id": None,
                    "symbol": None,
                    "start_date": start_date,
                    "end_date": end_date,
                }
            ]

    plan_record = DataSyncPlan(
        id=uuid4().hex,
        task_id=task_id,
        parent_plan_id=parent_plan_id,
        dataset=dataset,
        mode=str(frozen_plan.get("mode", "incremental")),
        source=str(payload.get("source", "watchlist")),
        status="queued",
        requested_start_date=start_date,
        requested_end_date=end_date,
        partition_strategy=str(frozen_plan.get("partition_strategy", "single_fetch")),
        total_partitions=len(partition_specs),
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    db.add(plan_record)
    # DataSyncPlan and DataSyncPartition deliberately have no ORM relationship.
    # Flush the parent explicitly before adding children so MySQL never observes
    # a partition whose plan_id is not yet present (SQLite can mask this order).
    db.flush([plan_record])
    for spec in partition_specs:
        db.add(
            DataSyncPartition(
                id=uuid4().hex,
                plan_id=plan_record.id,
                partition_key=str(spec["partition_key"]),
                symbol_id=spec.get("symbol_id"),
                symbol=spec.get("symbol"),
                start_date=_as_date(spec.get("start_date")) or start_date,
                end_date=_as_date(spec.get("end_date")) or end_date,
                status="queued",
            )
        )
    db.flush()
    return plan_record


def _refresh_sync_plan(db: Session, plan_id: str, *, terminal: bool = False) -> None:
    plan_record = db.get(DataSyncPlan, plan_id)
    if plan_record is None:
        return
    statuses = db.execute(
        select(DataSyncPartition.status).where(DataSyncPartition.plan_id == plan_id)
    ).scalars().all()
    plan_record.completed_partitions = sum(status == "done" for status in statuses)
    plan_record.skipped_partitions = sum(status == "skipped" for status in statuses)
    plan_record.failed_partitions = sum(status == "failed" for status in statuses)
    if terminal:
        plan_record.status = "partial" if plan_record.failed_partitions else "done"
        plan_record.finished_at = _now()
    elif plan_record.status == "queued":
        plan_record.status = "running"
        plan_record.started_at = _now()
    plan_record.updated_at = _now()
    db.commit()


def _set_partition_status(
    db: Session,
    partition: DataSyncPartition | None,
    status: str,
    *,
    rows_written: int = 0,
    error_message: str | None = None,
) -> None:
    if partition is None:
        return
    if status == "running":
        partition.attempts += 1
        partition.started_at = _now()
        partition.finished_at = None
    elif status in {"done", "skipped", "failed", "cancelled"}:
        partition.finished_at = _now()
    partition.status = status
    partition.rows_written = rows_written
    partition.error_message = error_message
    partition.updated_at = _now()
    db.commit()


def get_external_data_gaps(
    db: Session,
    *,
    dataset: Literal["fundamental", "financial", "capital_flow"],
    start_date: date,
    end_date: date,
    limit: int = 200,
) -> dict[str, Any]:
    """List real missing external rows against locally available A-share bars."""
    if dataset not in _GAP_REPAIR_DATASETS:
        raise ValueError("Gap repair is only available for valuation, financial reports and capital flow")
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if dataset == "financial":
        has_visible_report = (
            select(StockFinancialReport.id)
            .where(
                StockFinancialReport.symbol_id == Symbol.id,
                StockFinancialReport.announcement_date <= end_date,
                StockFinancialReport.roe_ttm.is_not(None),
            )
            .exists()
        )
        base = (
            select(Symbol.id, Symbol.symbol, func.max(DailyBar.trade_date))
            .join(DailyBar, DailyBar.symbol_id == Symbol.id)
            .where(
                Symbol.asset_type == "stock", Symbol.is_active == 1,
                DailyBar.trade_date >= start_date, DailyBar.trade_date <= end_date,
                ~has_visible_report,
            )
            .group_by(Symbol.id, Symbol.symbol)
        )
        total_missing = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
        rows = db.execute(base.order_by(Symbol.id).limit(limit)).all()
        return {
            "dataset": dataset, "start_date": start_date, "end_date": end_date,
            "total_missing": total_missing, "truncated": total_missing > len(rows),
            "semantics": "no_pit_financial_report_visible_by_end_date",
            "gaps": [
                {"symbol_id": int(row[0]), "symbol": str(row[1]), "trade_date": row[2]}
                for row in rows
            ],
        }
    source_model = StockValuation if dataset == "fundamental" else CapitalFlow
    source_id = source_model.id
    join_condition = and_(
        source_model.symbol_id == DailyBar.symbol_id,
        source_model.trade_date == DailyBar.trade_date,
    )
    base = (
        select(Symbol.id, Symbol.symbol, DailyBar.trade_date)
        .join(Symbol, Symbol.id == DailyBar.symbol_id)
        .outerjoin(source_model, join_condition)
        .where(
            Symbol.asset_type == "stock",
            Symbol.is_active == 1,
            DailyBar.trade_date >= start_date,
            DailyBar.trade_date <= end_date,
            source_id.is_(None),
        )
    )
    total_missing = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = db.execute(
        base.order_by(DailyBar.trade_date.desc(), Symbol.id).limit(limit)
    ).all()
    return {
        "dataset": dataset,
        "start_date": start_date,
        "end_date": end_date,
        "total_missing": total_missing,
        "truncated": total_missing > len(rows),
        "gaps": [
            {"symbol_id": int(row[0]), "symbol": str(row[1]), "trade_date": row[2]}
            for row in rows
        ],
    }


def start_external_gap_repair(
    dataset: Literal["fundamental", "financial", "capital_flow"],
    *,
    start_date: date,
    end_date: date,
    gaps: list[dict[str, Any]],
) -> AsyncTaskRead:
    """Freeze selected missing points into retryable symbol/date partitions."""
    if dataset not in _GAP_REPAIR_DATASETS:
        raise ValueError("Gap repair is only available for valuation, financial reports and capital flow")
    if not gaps:
        raise ValueError("No missing points were selected for repair")
    if len(gaps) > 1000:
        raise ValueError("At most 1000 missing points can be repaired per task")
    by_symbol: dict[tuple[int, str], list[date]] = {}
    for item in gaps:
        try:
            symbol_id = int(item["symbol_id"])
            symbol = str(item["symbol"])
            trade_date = _as_date(item["trade_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Gap item is invalid") from exc
        if trade_date is None or trade_date < start_date or trade_date > end_date:
            raise ValueError("Gap date is outside the selected repair range")
        by_symbol.setdefault((symbol_id, symbol), []).append(trade_date)
    partition_specs = [
        {
            "partition_key": f"gap:{symbol_id}:{min(days)}:{max(days)}",
            "symbol_id": symbol_id,
            "symbol": symbol,
            "start_date": min(days),
            "end_date": max(days),
        }
        for (symbol_id, symbol), days in sorted(by_symbol.items())
    ]
    if dataset == "financial":
        # Financial records are revisioned by announcement date. A missing
        # PIT baseline must re-fetch the symbol's complete published history,
        # not fabricate one row for each missing daily-bar date.
        return start_external_data_sync(
            dataset,
            {
                "source": "all", "mode": "incremental", "end_date": end_date,
                "lookback_days": 1, "limit": 20,
                "_partition_specs": partition_specs,
            },
        )
    return start_external_data_sync(
        dataset,
        {
            "source": "all",
            "mode": "backfill",
            "start_date": start_date,
            "end_date": end_date,
            "lookback_days": (end_date - start_date).days + 1,
            "limit": 20,
            "_partition_specs": partition_specs,
        },
    )


_DATASET_FORMULA_FIELDS: dict[ExternalDataset, tuple[str, ...]] = {
    "fundamental": ("pe_ttm", "pb"),
    "financial": ("roe_ttm",),
    "capital_flow": ("main_net_inflow",),
    "lhb": ("lhb_institution_net",),
    "hot_rank": ("hot_rank_pct",),
    "tail_proxy": ("proxy_score",),
    "etf": ("etf_premium_discount", "etf_tracking_error", "etf_fund_size"),
}


def get_external_data_coverage(db: Session) -> dict[str, Any]:
    """Expose cached warehouse-backed field coverage for the external-data UI."""
    try:
        from app.services.factors.config import get_factor_system_config
        from app.services.factors.formula_catalog import build_formula_catalog
        from app.services.factors.store import FactorWarehouse

        config = get_factor_system_config(db)
        catalog = build_formula_catalog(FactorWarehouse(config.warehouse_path))
        fields_by_key = {item["key"]: item for item in catalog["fields"]}
    except Exception as exc:
        logger.warning("external coverage read failed: %s", exc)
        return {
            "datasets": [
                {
                    "dataset": dataset,
                    "readiness": "unknown",
                    "reason": "factor_warehouse_unavailable",
                    "fields": [],
                }
                for dataset in EXTERNAL_DATASETS
            ],
            "generated_at": _now(),
        }

    datasets: list[dict[str, Any]] = []
    for dataset in EXTERNAL_DATASETS:
        selected_fields = [
            fields_by_key[field]
            for field in _DATASET_FORMULA_FIELDS[dataset]
            if field in fields_by_key
        ]
        field_items = [
            {
                "field": item["key"],
                "availability": item["availability"],
                "evaluation_enabled": item["evaluation_enabled"],
                "first_date": item["first_date"],
                "latest_date": item["latest_date"],
                "nonnull_rows": item["nonnull_rows"],
                "table_rows": item["table_rows"],
                "distinct_symbols": item["distinct_symbols"],
                "distinct_dates": item["distinct_dates"],
                "continuity_days": item.get("continuity_days", item["distinct_dates"]),
                "daily_coverage_p50": item.get("daily_coverage_p50"),
                "daily_coverage_p90": item.get("daily_coverage_p90"),
                "latest_daily_coverage": item.get("latest_daily_coverage"),
                "reason": item["status_reason"],
            }
            for item in selected_fields
        ]
        availability = [str(item["availability"]) for item in selected_fields]
        if not selected_fields:
            readiness, reason = "not_applicable", "no_factor_field_mapping"
        elif "blocked" in availability or "unknown" in availability:
            readiness, reason = "blocked", "field_data_unavailable"
        elif all(item == "available" for item in availability):
            readiness, reason = "available", "all_fields_evaluation_ready"
        elif "event" in availability:
            readiness, reason = "event", "event_only_data"
        elif "snapshot" in availability:
            readiness, reason = "snapshot", "snapshot_only_data"
        else:
            readiness, reason = "limited", "coverage_or_pit_limited"
        datasets.append(
            {
                "dataset": dataset,
                "readiness": readiness,
                "reason": reason,
                "fields": field_items,
            }
        )
    return {"datasets": datasets, "generated_at": _now()}


def _run_symbol_sync(
    db: Session,
    task_id: str,
    dataset: Literal["fundamental", "financial", "capital_flow", "etf"],
    payload: dict,
) -> dict | None:
    plan = payload.get("plan") or build_external_sync_plan(dataset, payload)
    range_start = _as_date(plan.get("requested_start_date")) or date.today()
    range_end = _as_date(plan.get("requested_end_date")) or date.today()
    source = payload.get("source", "watchlist")
    asset_type = "etf" if dataset == "etf" else "stock"
    sync_plan_id = str(payload.get("sync_plan_id") or "")
    partitions_by_symbol: dict[int, DataSyncPartition] = {}
    if sync_plan_id:
        partitions = db.execute(
            select(DataSyncPartition)
            .where(
                DataSyncPartition.plan_id == sync_plan_id,
                DataSyncPartition.status.in_(("queued", "failed", "cancelled")),
                DataSyncPartition.symbol_id.is_not(None),
            )
            .order_by(DataSyncPartition.created_at, DataSyncPartition.id)
        ).scalars().all()
        symbol_ids = [int(partition.symbol_id) for partition in partitions if partition.symbol_id is not None]
        symbols_by_id = {
            symbol.id: symbol
            for symbol in db.execute(select(Symbol).where(Symbol.id.in_(symbol_ids))).scalars().all()
        } if symbol_ids else {}
        symbols = [symbols_by_id[symbol_id] for symbol_id in symbol_ids if symbol_id in symbols_by_id]
        partitions_by_symbol = {
            int(partition.symbol_id): partition
            for partition in partitions
            if partition.symbol_id is not None
        }
        _refresh_sync_plan(db, sync_plan_id)
    else:
        symbols = resolve_external_symbols(db, source, asset_type, payload.get("watchlist_id"))
        resume_after_symbol_id = payload.get("resume_after_symbol_id")
        if resume_after_symbol_id is not None:
            try:
                cursor = int(resume_after_symbol_id)
                symbols = [symbol for symbol in symbols if symbol.id > cursor]
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid external sync cursor: %r", resume_after_symbol_id)
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

    # The provider already returns the complete A-share snapshot. Fetch it
    # once for incremental valuation syncs instead of repeating the same
    # full-market request for every symbol.
    if dataset == "fundamental" and plan.get("mode") != "backfill":
        from app.services.fundamental_data import sync_market_valuation_snapshot

        if _is_cancelled(db, task_id):
            return None
        try:
            synced_ids = sync_market_valuation_snapshot(db, symbols, range_end) or set()
        except Exception as exc:
            db.rollback()
            synced_ids = set()
            result["failed"] = max(len(symbols), 1)
            result["errors"].append(f"provider: {type(exc).__name__}: {exc}")
            logger.warning("fundamental snapshot sync failed: %s", exc, exc_info=True)
        result["success"] = len(synced_ids)
        result["skipped"] = max(len(symbols) - len(synced_ids), 0) if not result["failed"] else 0
        result["records"] = len(synced_ids)
        for symbol_id, partition in partitions_by_symbol.items():
            partition.status = "done" if symbol_id in synced_ids else "skipped"
            partition.attempts = max(partition.attempts, 1)
            partition.rows_written = 1 if symbol_id in synced_ids else 0
            partition.finished_at = _now()
            partition.updated_at = _now()
        db.commit()
        if _is_cancelled(db, task_id):
            return None
        _update_task(
            db,
            task_id,
            status="running",
            stage="sync",
            percent=99,
            processed=len(symbols),
            ok_count=result["success"],
            failed_count=0,
            current_item=None,
            message=_progress_message(dataset, len(symbols), len(symbols)),
        )
        if sync_plan_id:
            _refresh_sync_plan(db, sync_plan_id, terminal=True)
        return result

    if dataset == "capital_flow" and payload.get("include_northbound", True):
        from app.services.capital_flow_data import sync_northbound_flow

        _update_task(db, task_id, stage="northbound", percent=4, message="Syncing northbound flow")
        try:
            sync_northbound_flow(
                db, days=min(max((range_end - range_start).days + 1, 1), 100)
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"northbound: {exc}")
            logger.warning("northbound sync failed in external task: %s", exc)

    # Historical valuation requests are independent per symbol. Run them in
    # bounded batches with one SQLAlchemy session per worker; the coordinator
    # remains responsible for task progress, partition state and cancellation.
    if dataset == "fundamental" and plan.get("mode") == "backfill" and symbols:
        from app.services.fundamental_data import sync_symbol_valuation_range

        max_workers = min(max(int(payload.get("max_workers", 4)), 1), 8)

        def _backfill_worker(symbol_id: int, start: date, end: date) -> tuple[int, int, str | None]:
            worker_db = SessionLocal()
            try:
                worker_symbol = worker_db.get(Symbol, symbol_id)
                if worker_symbol is None:
                    return symbol_id, 0, "symbol not found"
                count = sync_symbol_valuation_range(
                    worker_db,
                    worker_symbol,
                    start_date=start,
                    end_date=end,
                    quiet_output=False,
                )
                worker_db.commit()
                return symbol_id, count, None
            except Exception as exc:
                worker_db.rollback()
                return symbol_id, 0, str(exc)
            finally:
                worker_db.close()

        processed = 0
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="valuation-sync") as executor:
            for batch_start in range(0, len(symbols), max_workers):
                if _is_cancelled(db, task_id):
                    return None
                batch = symbols[batch_start : batch_start + max_workers]
                futures = {
                    executor.submit(
                        _backfill_worker,
                        symbol.id,
                        partitions_by_symbol.get(symbol.id).start_date
                        if partitions_by_symbol.get(symbol.id) is not None
                        else range_start,
                        partitions_by_symbol.get(symbol.id).end_date
                        if partitions_by_symbol.get(symbol.id) is not None
                        else range_end,
                    ): symbol
                    for symbol in batch
                }
                for future in as_completed(futures):
                    symbol = futures[future]
                    partition = partitions_by_symbol.get(symbol.id)
                    try:
                        _, count, error = future.result()
                    except Exception as exc:  # defensive: worker normally returns errors
                        count, error = 0, str(exc)
                    processed += 1
                    if error:
                        result["failed"] += 1
                        if len(result["errors"]) < 20:
                            result["errors"].append(f"{symbol.symbol}: {error}")
                        _set_partition_status(db, partition, "failed", error_message=error)
                    elif count > 0:
                        result["success"] += 1
                        result["records"] += count
                        _set_partition_status(db, partition, "done", rows_written=count)
                    else:
                        result["skipped"] += 1
                        _set_partition_status(db, partition, "skipped")
                    db.commit()
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
                        batch_recovery_json=json.dumps({
                            "plan": plan,
                            "last_symbol_id": symbol.id,
                            "processed": processed,
                            "total": len(symbols),
                        }, ensure_ascii=False),
                    )
        if sync_plan_id:
            _refresh_sync_plan(db, sync_plan_id, terminal=True)
        return result

    for index, symbol in enumerate(symbols):
        if _is_cancelled(db, task_id):
            return None
        if not _wait_for_market_priority(db, task_id):
            return None
        partition = partitions_by_symbol.get(symbol.id)
        partition_start = partition.start_date if partition is not None else range_start
        partition_end = partition.end_date if partition is not None else range_end
        _set_partition_status(db, partition, "running")
        try:
            if dataset == "fundamental":
                from app.services.fundamental_data import sync_symbol_valuation

                if plan.get("mode") == "backfill":
                    from app.services.fundamental_data import sync_symbol_valuation_range

                    count = sync_symbol_valuation_range(
                        db, symbol, start_date=partition_start, end_date=partition_end
                    )
                else:
                    item = sync_symbol_valuation(db, symbol, range_end)
                    count = 1 if item is not None else 0
            elif dataset == "financial":
                from app.services.financial_data import sync_symbol_financial_reports

                if plan.get("mode") == "backfill":
                    count = sync_symbol_financial_reports(
                        db,
                        symbol,
                        announcement_start=partition_start,
                        announcement_end=partition_end,
                    )
                else:
                    count = sync_symbol_financial_reports(db, symbol)
            elif dataset == "capital_flow":
                from app.services.capital_flow_data import sync_symbol_capital_flow

                if plan.get("mode") == "backfill":
                    from app.services.capital_flow_data import sync_symbol_capital_flow_range

                    count = sync_symbol_capital_flow_range(
                        db, symbol, start_date=partition_start, end_date=partition_end
                    )
                else:
                    item = sync_symbol_capital_flow(db, symbol, range_end)
                    count = 1 if item is not None else 0
            else:
                from app.services.etf_basic_data import sync_etf_indicator

                item = sync_etf_indicator(db, symbol, range_end)
                count = 1 if item is not None else 0

            if count > 0:
                result["success"] += 1
                result["records"] += count
                _set_partition_status(db, partition, "done", rows_written=count)
            else:
                result["skipped"] += 1
                _set_partition_status(db, partition, "skipped")
            db.commit()
        except Exception as exc:
            db.rollback()
            result["failed"] += 1
            if partition is not None:
                partition = db.get(DataSyncPartition, partition.id)
            _set_partition_status(
                db,
                partition,
                "failed",
                error_message=str(exc),
            )
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
            batch_recovery_json=json.dumps({
                "plan": plan,
                "last_symbol_id": symbol.id,
                "processed": processed,
                "total": len(symbols),
            }, ensure_ascii=False),
        )
        if index < len(symbols) - 1:
            time.sleep(_SYNC_THROTTLE_SECONDS)
    if sync_plan_id:
        _refresh_sync_plan(db, sync_plan_id, terminal=True)
    return result


def _run_bulk_sync(
    db: Session,
    task_id: str,
    dataset: Literal["lhb", "hot_rank", "tail_proxy"],
    payload: dict,
) -> dict:
    if not _wait_for_market_priority(db, task_id):
        return {
            "dataset": dataset,
            "total": 0,
            "success": 0,
            "skipped": 0,
            "failed": 0,
            "records": 0,
            "errors": ["cancelled while waiting for market-data priority"],
        }
    sync_plan_id = str(payload.get("sync_plan_id") or "")
    partition = None
    if sync_plan_id:
        partition = db.execute(
            select(DataSyncPartition)
            .where(
                DataSyncPartition.plan_id == sync_plan_id,
                DataSyncPartition.status.in_(("queued", "failed", "cancelled")),
            )
            .order_by(DataSyncPartition.created_at, DataSyncPartition.id)
        ).scalars().first()
        _refresh_sync_plan(db, sync_plan_id)
        _set_partition_status(db, partition, "running")
    _update_task(
        db,
        task_id,
        status="running",
        stage="fetch",
        percent=10,
        message=f"Fetching {dataset} data from source",
        started_at=_now(),
    )
    try:
        if dataset == "lhb":
            from app.services.lhb_data import sync_lhb_institution_trades

            plan = payload.get("plan") or build_external_sync_plan(dataset, payload)
            start_date = _as_date(plan.get("requested_start_date")) or date.today()
            end_date = _as_date(plan.get("requested_end_date")) or date.today()
            summary = sync_lhb_institution_trades(db, start_date=start_date, end_date=end_date)
            result = {
                "dataset": dataset, "total": summary.received,
                "success": summary.written, "skipped": summary.unmatched,
                "failed": 0, "records": summary.written, "errors": [],
            }
        elif dataset == "hot_rank":
            from app.services.hot_rank_data import sync_hot_rank_snapshot

            summary = sync_hot_rank_snapshot(db)
            result = {
                "dataset": dataset, "total": summary.received,
                "success": summary.written, "skipped": summary.unmatched,
                "failed": 0, "records": summary.written, "errors": [],
            }
        else:
            from app.services.tail_proxy_data import sync_tail_proxy_snapshots

            summary = sync_tail_proxy_snapshots(
                db, source=str(payload.get("source") or "candidates"),
                limit=int(payload.get("limit", 20)),
            )
            result = {
                "dataset": dataset, "total": summary.total,
                "success": summary.written, "skipped": summary.skipped,
                "failed": summary.failed, "records": summary.written,
                "errors": list(summary.errors),
            }
    except Exception as exc:
        # Provider outages and schema drift should remain inspectable in the
        # task result instead of collapsing into a vague top-level failure.
        db.rollback()
        result = {
            "dataset": dataset,
            "total": 0,
            "success": 0,
            "skipped": 0,
            "failed": 1,
            "records": 0,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
        logger.warning("%s provider sync failed: %s", dataset, exc, exc_info=True)
    # Provider work is complete; expose the finalization phase explicitly so
    # a task cannot look frozen at the initial fetch checkpoint.
    _update_task(
        db,
        task_id,
        status="running",
        stage="sync",
        percent=95,
        total=result["total"],
        processed=result["success"] + result["skipped"] + result["failed"],
        ok_count=result["success"],
        failed_count=result["failed"],
        current_item=None,
        message=("Provider sync finished; finalizing results" if not result["failed"]
                 else "Provider sync finished with errors; finalizing results"),
    )
    db.commit()
    if result["failed"]:
        _set_partition_status(
            db,
            partition,
            "failed",
            rows_written=result["records"],
            error_message="; ".join(result["errors"][-5:]) or "bulk sync failed",
        )
    elif result["records"] > 0:
        _set_partition_status(db, partition, "done", rows_written=result["records"])
    else:
        _set_partition_status(db, partition, "skipped")
    if sync_plan_id:
        _refresh_sync_plan(db, sync_plan_id, terminal=True)
    return result


def _mirror_external_factor_inputs(
    db: Session,
    task_id: str,
    dataset: ExternalDataset,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Mirror successful external writes so formula execution sees the new data.

    The mirror is local-only. A mirror failure is recorded as a warning instead
    of rolling back the already durable provider data, which keeps retry safe.
    """
    mirror_flags: dict[ExternalDataset, dict[str, bool] | None] = {
        "fundamental": {"include_valuations": True},
        "financial": {"include_financial_reports": True},
        "capital_flow": {"include_fund_flows": True},
        "lhb": {"include_sentiment": True},
        "hot_rank": {"include_sentiment": True},
        "tail_proxy": {"include_tail_proxy": True},
        "etf": {"include_etf_indicators": True},
    }
    flags = mirror_flags[dataset]
    if flags is None:
        return {"status": "not_applicable", "records": 0}
    start_date = _as_date(plan.get("requested_start_date"))
    end_date = _as_date(plan.get("requested_end_date"))
    _update_task(
        db,
        task_id,
        stage="mirror",
        percent=99,
        message="Mirroring external data into factor warehouse",
    )
    try:
        from app.services.factors.data_sync import mirror_factor_inputs

        mirror_kwargs = {
            "include_valuations": False,
            "include_financial_reports": False,
            "include_fund_flows": False,
            "include_sentiment": False,
            "include_tail_proxy": False,
            "include_etf_indicators": False,
            "include_macro": False,
        }
        mirror_kwargs.update(flags)
        mirror_result = mirror_factor_inputs(
            db,
            start_date=start_date,
            end_date=end_date,
            **mirror_kwargs,
        )
        return {
            "status": "done",
            "records": mirror_result.rows_written,
            "batch_id": mirror_result.batch_id,
        }
    except Exception as exc:
        logger.warning("factor warehouse mirror failed for external task %s: %s", task_id, exc)
        return {"status": "warning", "records": 0, "error": str(exc)}


def _run_external_sync_task(task_id: str, dataset: ExternalDataset, payload: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        if dataset in ("fundamental", "financial", "capital_flow", "etf"):
            result = _run_symbol_sync(db, task_id, dataset, payload)
        else:
            result = _run_bulk_sync(db, task_id, dataset, payload)
        if result is None or _is_cancelled(db, task_id):
            _mark_cancelled(db, task_id)
            return
        plan = payload.get("plan") or build_external_sync_plan(dataset, payload)
        result["plan"] = plan
        if _is_cancelled(db, task_id):
            _mark_cancelled(db, task_id)
            return
        mirror = _mirror_external_factor_inputs(db, task_id, dataset, plan)
        result["warehouse_mirror"] = mirror
        if mirror.get("status") == "warning":
            result["errors"].append(f"warehouse_mirror: {mirror['error']}")
        if _is_cancelled(db, task_id):
            _mark_cancelled(db, task_id)
            return
        try:
            from app.services.data_quality import capture_field_quality_snapshots

            result["quality_snapshot"] = capture_field_quality_snapshots(
                db, trigger=f"external_sync:{dataset}"
            )
        except Exception as exc:
            logger.warning("quality snapshot failed after external task %s: %s", task_id, exc)
            result["errors"].append(f"quality_snapshot: {exc}")
        processed = result["success"] + result["skipped"] + result["failed"]
        if _is_cancelled(db, task_id):
            _mark_cancelled(db, task_id)
            return
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
        # Explicitly persist the terminal lock so startup patrol cannot claim
        # a successfully completed external task after a late refresh.
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            task.is_terminal_locked = 1
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("external data task %s failed", task_id)
        sync_plan_id = str(payload.get("sync_plan_id") or "")
        if sync_plan_id:
            plan_record = db.get(DataSyncPlan, sync_plan_id)
            if plan_record is not None:
                running_partitions = db.execute(
                    select(DataSyncPartition).where(
                        DataSyncPartition.plan_id == sync_plan_id,
                        DataSyncPartition.status == "running",
                    )
                ).scalars().all()
                for partition in running_partitions:
                    partition.status = "failed"
                    partition.error_message = str(exc)
                    partition.finished_at = _now()
                    partition.updated_at = _now()
                plan_record.status = "failed"
                plan_record.failed_partitions = max(
                    plan_record.failed_partitions, len(running_partitions)
                )
                plan_record.finished_at = _now()
                plan_record.updated_at = _now()
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None and task.status not in ("done", "failed", "cancelled"):
            task.status = "failed"
            task.stage = "failed"
            # A terminal failure must not leave the UI at the fetch-stage 10%
            # checkpoint; 100% here means the task lifecycle is finished.
            task.percent = 100
            task.total = task.total or 0
            task.processed = task.processed or 0
            task.ok_count = task.ok_count or 0
            task.failed_count = max(task.failed_count or 0, 1)
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


def start_external_data_sync(dataset: ExternalDataset, payload: dict[str, Any]) -> AsyncTaskRead:
    if dataset not in EXTERNAL_DATASETS:
        raise ValueError(f"Unsupported external dataset: {dataset}")
    plan = build_external_sync_plan(dataset, payload)
    parent_plan_id = payload.pop("_parent_plan_id", None)
    partition_specs = payload.pop("_partition_specs", None)
    task_payload = {"dataset": dataset, **payload, "plan": plan}
    db = SessionLocal()
    try:
        # Different datasets may run concurrently.  Keep only the same dataset
        # serialized so two requests cannot write the same external table at once.
        existing = db.execute(
            select(AsyncTaskRecord)
            .where(
                AsyncTaskRecord.task_type == _TASK_TYPES[dataset],
                AsyncTaskRecord.status.in_(("queued", "running")),
            )
            .order_by(desc(AsyncTaskRecord.created_at))
        ).scalars().first()
        if existing is not None:
            raise RuntimeError("Another external-data sync task is already running")
        market_task = _active_market_priority_task(db)
        if market_task is not None:
            raise RuntimeError(
                "Market-data synchronization has priority; wait for "
                f"{market_task.task_type} to finish before starting external data"
            )

        task_id = uuid4().hex
        task = AsyncTaskRecord(
            id=task_id,
            task_type=_TASK_TYPES[dataset],
            status="queued",
            stage="queued",
            percent=0,
            message="External-data sync task created",
            payload_json=json.dumps(task_payload, ensure_ascii=False, default=str),
            batch_recovery_json=json.dumps({"plan": plan, "processed": 0}, ensure_ascii=False),
        )
        db.add(task)
        db.flush()
        persisted_plan = _create_persisted_sync_plan(
            db,
            task_id=task_id,
            dataset=dataset,
            payload=task_payload,
            frozen_plan=plan,
            parent_plan_id=parent_plan_id,
            partition_specs=partition_specs,
        )
        task_payload["sync_plan_id"] = persisted_plan.id
        task.payload_json = json.dumps(task_payload, ensure_ascii=False, default=str)
        task.batch_recovery_json = json.dumps(
            {"plan": plan, "sync_plan_id": persisted_plan.id, "processed": 0},
            ensure_ascii=False,
        )
        db.commit()
    finally:
        db.close()

    worker = threading.Thread(
        target=_run_external_sync_task,
        args=(task_id, dataset, task_payload),
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


def get_external_sync_partitions(task_id: str) -> dict[str, Any] | None:
    """Return the durable plan and partitions for task diagnostics."""
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.task_type not in _DATASET_BY_TASK_TYPE:
            return None
        plan_record = db.execute(
            select(DataSyncPlan)
            .where(DataSyncPlan.task_id == task_id)
            .order_by(desc(DataSyncPlan.created_at))
        ).scalars().first()
        if plan_record is None:
            return {
                "task_id": task_id,
                "plan": None,
                "partitions": [],
            }
        partitions = db.execute(
            select(DataSyncPartition)
            .where(DataSyncPartition.plan_id == plan_record.id)
            .order_by(DataSyncPartition.created_at, DataSyncPartition.id)
        ).scalars().all()
        return {
            "task_id": task_id,
            "plan": {
                "id": plan_record.id,
                "parent_plan_id": plan_record.parent_plan_id,
                "dataset": plan_record.dataset,
                "mode": plan_record.mode,
                "source": plan_record.source,
                "status": plan_record.status,
                "requested_start_date": plan_record.requested_start_date,
                "requested_end_date": plan_record.requested_end_date,
                "partition_strategy": plan_record.partition_strategy,
                "total_partitions": plan_record.total_partitions,
                "completed_partitions": plan_record.completed_partitions,
                "skipped_partitions": plan_record.skipped_partitions,
                "failed_partitions": plan_record.failed_partitions,
                "created_at": plan_record.created_at,
                "started_at": plan_record.started_at,
                "finished_at": plan_record.finished_at,
            },
            "partitions": [
                {
                    "id": partition.id,
                    "partition_key": partition.partition_key,
                    "symbol_id": partition.symbol_id,
                    "symbol": partition.symbol,
                    "start_date": partition.start_date,
                    "end_date": partition.end_date,
                    "status": partition.status,
                    "attempts": partition.attempts,
                    "rows_written": partition.rows_written,
                    "error_message": partition.error_message,
                    "started_at": partition.started_at,
                    "finished_at": partition.finished_at,
                }
                for partition in partitions
            ],
        }
    finally:
        db.close()


def cancel_external_data_sync(task_id: str) -> AsyncTaskRead:
    """Cancel an external task while keeping its persisted recovery cursor."""
    task = get_external_sync_task(task_id)
    if task is None:
        raise ValueError("External-data sync task not found")
    cancelled = cancel_async_task(task_id)
    db = SessionLocal()
    try:
        plan_record = db.execute(
            select(DataSyncPlan)
            .where(DataSyncPlan.task_id == task_id)
            .order_by(desc(DataSyncPlan.created_at))
        ).scalars().first()
        if plan_record is not None:
            partitions = db.execute(
                select(DataSyncPartition).where(
                    DataSyncPartition.plan_id == plan_record.id,
                    DataSyncPartition.status.in_(("queued", "running")),
                )
            ).scalars().all()
            for partition in partitions:
                partition.status = "cancelled"
                partition.finished_at = _now()
                partition.updated_at = _now()
            plan_record.status = "cancelled"
            plan_record.finished_at = _now()
            plan_record.updated_at = _now()
            db.commit()
    finally:
        db.close()
    return cancelled


def retry_external_data_sync(task_id: str) -> AsyncTaskRead:
    """Create a new task containing only failed or unfinished partitions."""
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.task_type not in _DATASET_BY_TASK_TYPE:
            raise ValueError("External-data sync task not found")
        if task.status not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled external-data tasks can be retried")
        try:
            payload = json.loads(task.payload_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("External-data task payload is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("External-data task payload is invalid")
        recovery = {}
        try:
            recovery = json.loads(task.batch_recovery_json or "{}")
        except json.JSONDecodeError:
            logger.warning("External task %s has invalid recovery metadata", task_id)
        original_plan = db.execute(
            select(DataSyncPlan)
            .where(DataSyncPlan.task_id == task_id)
            .order_by(desc(DataSyncPlan.created_at))
        ).scalars().first()
        retry_specs: list[dict[str, Any]] = []
        if original_plan is not None:
            retry_partitions = db.execute(
                select(DataSyncPartition)
                .where(
                    DataSyncPartition.plan_id == original_plan.id,
                    DataSyncPartition.status.in_(("failed", "cancelled", "queued", "running")),
                )
                .order_by(DataSyncPartition.created_at, DataSyncPartition.id)
            ).scalars().all()
            retry_specs = [
                {
                    "partition_key": partition.partition_key,
                    "symbol_id": partition.symbol_id,
                    "symbol": partition.symbol,
                    "start_date": partition.start_date,
                    "end_date": partition.end_date,
                }
                for partition in retry_partitions
            ]
        if retry_specs:
            payload["_parent_plan_id"] = original_plan.id if original_plan else None
            payload["_partition_specs"] = retry_specs
            payload.pop("resume_after_symbol_id", None)
        elif isinstance(recovery, dict) and recovery.get("last_symbol_id") is not None:
            # Compatibility fallback for tasks created before partition persistence.
            payload["resume_after_symbol_id"] = recovery["last_symbol_id"]
        frozen_plan = payload.get("plan")
        if isinstance(frozen_plan, dict):
            payload["start_date"] = frozen_plan.get("requested_start_date")
            payload["end_date"] = frozen_plan.get("requested_end_date")
            payload["lookback_days"] = frozen_plan.get("requested_span_days")
        dataset = _DATASET_BY_TASK_TYPE[task.task_type]
        payload.pop("dataset", None)
        payload.pop("sync_plan_id", None)
        return start_external_data_sync(dataset, payload)
    finally:
        db.close()


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
