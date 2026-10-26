"""Observable asynchronous tasks for external-data synchronization."""
from __future__ import annotations

import json
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import and_, desc, distinct, func, or_, select, text, update
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
from app.services.feature_policy import reject_tail_proxy
from app.services.external_sync_policy import resolve_policy_ranges, requested_days, sync_policy, subtract_ranges

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
    "capital_flow",
    "etf",
)

_TASK_TYPES = {dataset: f"external_sync_{dataset}" for dataset in (*EXTERNAL_DATASETS, "tail_proxy")}
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
_CREATION_LOCKS = {dataset: threading.Lock() for dataset in EXTERNAL_DATASETS}


@contextmanager
def _external_sync_creation_guard(db: Session, dataset: ExternalDataset):
    """Serialize ownership check+commit across threads and database clients."""
    process_lock = _CREATION_LOCKS[dataset]
    if not process_lock.acquire(blocking=False):
        raise RuntimeError("已有同类型外部数据同步任务在创建或运行，请稍后再试")
    lock_connection = None
    mysql_locked = False
    bind = db.get_bind()
    database_name = str(getattr(getattr(bind, "url", None), "database", "default"))
    namespace = hashlib.sha256(database_name.encode("utf-8")).hexdigest()[:12]
    lock_name = f"external-sync-create:{namespace}:{dataset}"
    try:
        dialect = bind.dialect.name
        if dialect == "mysql":
            # Named locks belong to a connection, not a transaction. Keep a
            # dedicated connection until the task transaction has committed.
            lock_connection = bind.connect()
            acquired = lock_connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}).scalar()
            if acquired != 1:
                raise RuntimeError("已有同类型外部数据同步任务在创建，请稍后再试")
            mysql_locked = True
        elif dialect == "sqlite":
            if db.in_transaction():
                raise RuntimeError("外部同步创建必须使用独立事务")
            db.execute(text("BEGIN IMMEDIATE"))
        else:
            raise RuntimeError(f"External sync creation locking unsupported for {dialect}")
        yield
    finally:
        try:
            if lock_connection is not None:
                try:
                    if mysql_locked:
                        lock_connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                except Exception:
                    # Never put a connection retaining GET_LOCK into the pool.
                    lock_connection.invalidate()
                    logger.warning("external sync creation lock release failed", exc_info=True)
                finally:
                    lock_connection.close()
        finally:
            process_lock.release()

ExternalSyncMode = Literal["incremental", "backfill"]

_DATASET_CAPABILITIES: dict[ExternalDataset, dict[str, Any]] = {
    "fundamental": {"modes": {"incremental", "backfill"}, "history_limit_days": None, "reason": "当天仅请求缺失标的及指定交易日；历史回填在服务端按起止日期过滤。接口未返回的日期不会填补，当天失败不会切换历史下载。"},
    "financial": {"modes": {"incremental", "backfill"}, "history_limit_days": None, "reason": "日更仅请求所选公告日期的新报告，不重复下载全历史；首次历史初始化请使用历史回填。财报可在休市日公告。"},
    "capital_flow": {"modes": {"incremental", "backfill"}, "history_limit_days": 100, "reason": "提供商每次返回近约 120 个交易日（2026-10-01 实测 120 行）；当日仅按所选标的批量获取当日资金流，不下载历史；历史回填才使用近约 120 个交易日的个股接口。"},
    "lhb": {"modes": {"incremental", "backfill"}, "history_limit_days": 31, "chunk_days": 31, "reason": "龙虎榜为事件数据，单次请求最多 31 个自然日；更长历史区间会自动分段执行。"},
    "hot_rank": {"modes": {"incremental"}, "history_limit_days": 0, "reason": "人气榜接口仅提供当前快照。"},
    "etf": {"modes": {"incremental", "backfill"}, "history_limit_days": None,
            "partial_history_fields": ["nav", "close", "premium_discount", "premium_discount_score"],
            "unavailable_history_fields": ["total_shares", "fund_size", "tracking_error"],
            "reason": "历史回填仅补净值、收盘价、溢折价和评分；份额、规模、跟踪误差只有当天快照。"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_external_symbols(
    db: Session,
    source: Literal["watchlist", "positions", "all"],
    asset_type: str | None,
    watchlist_id: int | None = None,
    portfolio_id: int | None = None,
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
        if asset_type == "etf":
            universe_stmt = universe_stmt.where(UniverseSymbol.region == "cn")
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
            select(Symbol).distinct()
            .join(Position, Position.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1, Position.quantity > 0)
        )
        if portfolio_id is not None:
            stmt = stmt.where(Position.portfolio_id == portfolio_id)
    else:
        return []
    if asset_type:
        stmt = stmt.where(Symbol.asset_type == asset_type)
    if asset_type == "etf":
        stmt = stmt.where(Symbol.market.in_(("sh", "sz", "bj", "cn")))
    return sorted(db.execute(stmt).scalars().all(), key=lambda symbol: symbol.id)


def _update_task(db: Session, task_id: str, **updates) -> AsyncTaskRecord | None:
    task = db.get(AsyncTaskRecord, task_id, populate_existing=True, with_for_update=True)
    if task is None:
        return None
    if task.status in ("done", "failed", "cancelled"):
        return task
    # cancel_async_task marks a running task with a cancellation token first;
    # do not let a late worker checkpoint move it forward while it is stopping.
    if int(getattr(task, "cancel_requested", 0) or 0) == 1 and updates.get("status") != "cancelled":
        return task
    for key, value in updates.items():
        if key == "started_at" and task.started_at is not None:
            continue
        setattr(task, key, value)
    if task.status in ("done", "failed", "cancelled"):
        task.is_terminal_locked = 1
        if task.finished_at is None:
            task.finished_at = _now()
        db.execute(update(DataSyncPlan).where(DataSyncPlan.task_id == task_id).values(
            status=task.status, started_at=task.started_at, finished_at=task.finished_at,
        ))
    task.updated_at = _now()
    if task.status in ("queued", "running"):
        task.heartbeat_at = _now()
        task.last_progress_at = task.last_progress_at or _now()
    db.commit()
    db.refresh(task)
    return task


def _is_cancelled(db: Session, task_id: str) -> bool:
    # A worker's ORM identity map / repeatable-read transaction must not hide
    # a cancellation committed by another request during a provider call.
    reader = SessionLocal()
    try:
        task = reader.get(AsyncTaskRecord, task_id)
        return task is None or task.status == "cancelled" or int(getattr(task, "cancel_requested", 0) or 0) == 1
    finally:
        reader.close()


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
    db.execute(update(DataSyncPlan).where(DataSyncPlan.task_id == task_id).values(
        status="cancelled", started_at=task.started_at, finished_at=task.finished_at,
    ))
    db.commit()


def _active_market_priority_task(db: Session) -> AsyncTaskRecord | None:
    """Read-only market-data priority gate; universe implementation stays untouched."""
    return db.execute(
        select(AsyncTaskRecord)
        .where(
            AsyncTaskRecord.task_type.in_(_MARKET_PRIORITY_TASK_TYPES),
            AsyncTaskRecord.status.in_(("queued", "running")),
            # A stale task cannot acquire provider capacity forever. Startup
            # patrol remains responsible for recording its recovery outcome.
            or_(AsyncTaskRecord.status != "running", AsyncTaskRecord.updated_at.is_(None), AsyncTaskRecord.updated_at >= _now() - timedelta(minutes=10)),
        )
        .execution_options(populate_existing=True)
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
    if dataset == "tail_proxy":
        reject_tail_proxy()
    if dataset not in EXTERNAL_DATASETS:
        raise ValueError(f"Unsupported external dataset: {dataset}")
    capability = _DATASET_CAPABILITIES[dataset]
    source = str(payload.get("source", "watchlist"))
    if source not in {"watchlist", "positions", "all", "candidates"}:
        raise ValueError("Unsupported external sync scope")
    if source == "candidates" and dataset != "tail_proxy":
        raise ValueError("Candidate scope is only supported for tail proxy")
    if dataset == "tail_proxy" and source == "all":
        raise ValueError("尾盘分钟采集仅支持候选、观察池或持仓范围，不支持全市场")
    mode = str(payload.get("mode", "incremental"))
    policy = sync_policy(payload)
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
    if requested_end > reference_date:
        raise ValueError("结束日期不能晚于今天，请勿选择未来日期")
    if mode == "incremental":
        if dataset in {"fundamental", "capital_flow", "etf", "hot_rank"} and requested_end != reference_date:
            if dataset in {"etf", "hot_rank"}:
                raise ValueError("实时快照仅支持当前日期，不能把实时行情用于历史日期")
            raise ValueError("估值和资金流增量仅支持当前日期；历史日期请使用历史回填模式")
        requested_start = requested_end
    span_days = (requested_end - requested_start).days + 1
    history_limit_days = capability["history_limit_days"]
    # A limit of zero denotes a current-snapshot-only provider. The one-day
    # incremental request is valid; only multi-day ranges should be rejected.
    # ``chunk_days`` marks providers whose per-request limit is not a total
    # history limit: longer ranges are split into consecutive safe windows.
    chunk_days = capability.get("chunk_days")
    if history_limit_days is not None and history_limit_days > 0 and span_days > history_limit_days and not chunk_days:
        raise ValueError(
            f"{dataset} requested {span_days} days exceeds provider limit {history_limit_days}: {capability['reason']}"
        )
    chunk_count = (span_days + chunk_days - 1) // chunk_days if chunk_days and span_days > chunk_days else 1
    return {
        "dataset": dataset,
        "mode": mode,
        "sync_policy": policy,
        "requested_start_date": requested_start.isoformat(),
        "requested_end_date": requested_end.isoformat(),
        "requested_span_days": span_days,
        "provider_history_limit_days": history_limit_days,
        "chunk_days": chunk_days,
        "chunk_count": chunk_count,
        "partial_history_fields": capability.get("partial_history_fields"),
        "unavailable_history_fields": capability.get("unavailable_history_fields"),
        "provider_reason": capability["reason"],
        "partition_strategy": "symbol_batches" if dataset in {"financial", "capital_flow", "fundamental", "etf"} else "single_fetch",
        "symbol_batch_size": min(max(int(payload.get("limit", 20)), 1), 50),
    }


def get_external_sync_capabilities() -> dict[str, dict[str, Any]]:
    result = {
        dataset: {
            "modes": sorted(values["modes"]),
            "history_limit_days": values["history_limit_days"],
            "chunk_days": values.get("chunk_days"),
            "partial_history_fields": values.get("partial_history_fields"),
            "unavailable_history_fields": values.get("unavailable_history_fields"),
            "reason": values["reason"],
        }
        for dataset, values in _DATASET_CAPABILITIES.items()
    }
    from app.services.capital_flow_data import northbound_capability
    result["capital_flow"]["northbound"] = northbound_capability()
    return result


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
                db, payload.get("source", "watchlist"), asset_type, payload.get("watchlist_id"), payload.get("portfolio_id")
            )
            if dataset == "fundamental" and frozen_plan.get("mode") != "backfill" and sync_policy(payload) == "normal":
                from app.services.fundamental_data import complete_daily_valuation_ids

                current_ids = complete_daily_valuation_ids(db, [symbol.id for symbol in symbols], end_date)
                payload["_already_current_count"] = len(current_ids)
                symbols = [symbol for symbol in symbols if symbol.id not in current_ids]
            ranges = resolve_policy_ranges(db, dataset, [symbol.id for symbol in symbols],
                                           start_date, end_date, sync_policy(payload))
            payload["_policy_ranges"] = {
                str(sid): [[a.isoformat(), b.isoformat()] for a, b in values]
                for sid, values in ranges.items()
            }
            payload["_covered_symbol_count"] = sum(not ranges[symbol.id] for symbol in symbols)
            symbols = [symbol for symbol in symbols if ranges[symbol.id]]
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
            if dataset == "capital_flow" and payload.get("include_northbound", True):
                partition_specs.append({"partition_key": f"northbound:{start_date}:{end_date}", "symbol_id": None, "symbol": None, "start_date": start_date, "end_date": end_date})
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
        sync_policy=sync_policy(payload),
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
        if "cancelled" in statuses:
            plan_record.status = "cancelled"
        else:
            plan_record.status = "partial" if plan_record.failed_partitions else "done"
        plan_record.finished_at = _now()
        if plan_record.started_at is None:
            plan_record.started_at = plan_record.finished_at
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
    db.refresh(partition)
    if partition.status == "cancelled" and status not in {"cancelled", "done"}:
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
    from app.services.market_session import cn_open_day_clause

    trading_day = cn_open_day_clause(db, DailyBar.trade_date)
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
                trading_day,
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
            trading_day,
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
        # A repair must respect the user's requested announcement range.
        # Earlier PIT baselines require an explicit earlier history request.
        for spec in partition_specs:
            spec["start_date"] = start_date
            spec["end_date"] = end_date
        return start_external_data_sync(
            dataset,
            {
                "source": "all", "mode": "backfill", "sync_policy": "missing",
                "start_date": start_date, "end_date": end_date,
                "lookback_days": (end_date - start_date).days + 1, "limit": 20,
                "_partition_specs": partition_specs,
            },
        )
    return start_external_data_sync(
        dataset,
        {
            "source": "all",
            "mode": "backfill",
            "sync_policy": "missing",
            # A frozen stock/date gap selection does not authorize unrelated
            # northbound market-wide snapshots.
            "include_northbound": False,
            "start_date": start_date,
            "end_date": end_date,
            "lookback_days": (end_date - start_date).days + 1,
            "limit": 20,
            "_partition_specs": partition_specs,
            "_selected_gap_dates": {str(sid): [day.isoformat() for day in sorted(set(days))]
                                    for (sid, _), days in by_symbol.items()},
        },
    )


_DATASET_FORMULA_FIELDS: dict[ExternalDataset, tuple[str, ...]] = {
    "fundamental": ("pe_ttm", "pb"),
    "financial": ("roe_ttm",),
    "capital_flow": ("main_net_inflow",),
    "lhb": ("lhb_institution_net",),
    "hot_rank": ("hot_rank_pct",),
    "etf": ("etf_premium_discount", "etf_tracking_error", "etf_fund_size"),
}


def get_external_data_coverage(db: Session) -> dict[str, Any]:
    """Expose cached warehouse-backed field coverage for the external-data UI."""
    try:
        from app.services.factors.config import get_factor_system_config# near-relative coupling: task scheduler reads scoring config to decide whether to enqueue — audit 2026-08-30
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
        # Fields that are intentionally not collected yet (for example
        # etf_tracking_error) stay visible in the field list but must not drag
        # the whole dataset into limited/partial readiness forever.
        readiness_fields = [
            item for item in selected_fields if not item.get("exclude_from_readiness")
        ]
        availability = [str(item["availability"]) for item in readiness_fields]
        if not selected_fields:
            readiness, reason = "not_applicable", "no_factor_field_mapping"
        elif not readiness_fields:
            readiness, reason = "not_applicable", "no_readiness_field"
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


def _merge_written_dates(result: dict, dates) -> None:
    actual = [_as_date(value) for value in dates if value is not None]
    if not actual:
        return
    previous = [_as_date(result.get("written_start_date")), _as_date(result.get("written_end_date"))]
    actual.extend(value for value in previous if value is not None)
    result["written_start_date"] = min(actual).isoformat()
    result["written_end_date"] = max(actual).isoformat()


def _local_written_dates(db: Session, dataset: ExternalDataset, symbol_id: int, start: date | None = None, end: date | None = None):
    model, column = {"fundamental": (StockValuation, StockValuation.trade_date), "financial": (StockFinancialReport, StockFinancialReport.announcement_date), "capital_flow": (CapitalFlow, CapitalFlow.trade_date), "etf": (EtfIndicator, EtfIndicator.trade_date)}[dataset]
    stmt = select(func.min(column), func.max(column)).where(model.symbol_id == symbol_id)
    if start is not None:
        stmt = stmt.where(column >= start)
    if end is not None:
        stmt = stmt.where(column <= end)
    return db.execute(stmt).one()


def _record_source_points(db, dataset, symbol_ids, start, end, task_id, *, allowed=None):
    """Record actual source points, never a synthetic min/max covered range."""
    from app.services.sync_coverage import existing_dates, source_covered_dates, record_coverage_points

    observed = existing_dates(db, dataset, symbol_ids, start, end)
    final = source_covered_dates(db, dataset, symbol_ids, start, end, require_complete=True)
    if allowed is not None:
        observed = {sid: days & allowed.get(sid, set()) for sid, days in observed.items()}
    complete = {sid: days & final.get(sid, set()) for sid, days in observed.items()}
    partial = {sid: days - final.get(sid, set()) for sid, days in observed.items()}
    record_coverage_points(db, dataset, complete, task_id=task_id, complete=True)
    record_coverage_points(db, dataset, partial, task_id=task_id, complete=False)


def _etf_partial_history_ranges(db, symbol_id: int, start: date, end: date, policy: str):
    """Skip days already carrying historical NAV, close and premium evidence."""
    if policy == "force":
        return [(start, end)]
    covered = set(db.execute(select(EtfIndicator.trade_date).where(
        EtfIndicator.symbol_id == symbol_id,
        EtfIndicator.trade_date >= start,
        EtfIndicator.trade_date <= end,
        EtfIndicator.nav.is_not(None),
        EtfIndicator.close.is_not(None),
        EtfIndicator.premium_discount.is_not(None),
    )).scalars())
    return subtract_ranges(requested_days(db, "etf", start, end), covered)


def _record_financial_scan(db, symbols, ranges, task_id):
    """Past announcement scans may be empty; today's announcements stay open."""
    from app.services.sync_coverage import record_coverage_points

    points = {symbol.id: {day for start, end in ranges.get(symbol.id, ())
                         for day in requested_days(db, "financial", start, end) if day < date.today()}
              for symbol in symbols}
    record_coverage_points(db, "financial", points, task_id=task_id, complete=True)


class _SymbolBatchCheckpoint:
    """Commit source rows, partition outcomes and progress together in batches."""
    def __init__(self, db, task_id, dataset, partitions, policy_ranges=None):
        self.db, self.task_id, self.dataset, self.partitions = db, task_id, dataset, partitions
        self.pending = []
        self.written_points = {}
        self.processed_symbols = []
        self.policy_ranges = policy_ranges or {}
        self.count = 0
        self.last = None
        self.updated_at = time.monotonic()

    def __call__(self, symbol, rows_written, summary):
        if self.dataset == "financial":
            self.processed_symbols.append(symbol)
        if rows_written:
            days = getattr(rows_written, "sync_written_dates", ()) if self.dataset == "financial" else (rows_written.trade_date,)
            self.written_points.setdefault(symbol.id, set()).update(day for day in days if day is not None)
        rows_written = int(rows_written is not None) if self.dataset == "etf" else int(rows_written)
        partition = self.partitions.get(symbol.id)
        if partition is not None:
            self.pending.append({"partition_id": partition.id,
                                 "sync_status": "done" if rows_written else "skipped",
                                 "sync_rows": rows_written})
        self.count += 1
        self.last = (symbol.symbol, dict(summary))
        if self.count >= 100 or time.monotonic() - self.updated_at >= 1:
            self.flush()

    def flush(self):
        if self.last is None:
            return
        from sqlalchemy import bindparam
        db, now = self.db, _now()
        db.flush()
        if self.processed_symbols:
            _record_financial_scan(db, self.processed_symbols, self.policy_ranges, self.task_id)
        if self.written_points:
            days = [day for values in self.written_points.values() for day in values]
            if days:
                _record_source_points(db, self.dataset, list(self.written_points), min(days), max(days),
                                      self.task_id, allowed=self.written_points)
        if self.pending:
            table = DataSyncPartition.__table__
            stmt = table.update().where(
                table.c.id == bindparam("partition_id"),
                or_(table.c.status != "cancelled", bindparam("sync_status") == "done"),
            ).values(status=bindparam("sync_status"), rows_written=bindparam("sync_rows"),
                     attempts=table.c.attempts + 1, started_at=func.coalesce(table.c.started_at, now),
                     finished_at=now, updated_at=now, error_message=None)
            db.execute(stmt, self.pending)
        symbol, summary = self.last
        processed = summary["success"] + summary["skipped"] + summary["failed"]
        task = _update_task(db, self.task_id, status="running", stage="sync",
            percent=min(99, 5 + 94 * processed / max(summary["total"], 1)),
            total=summary["total"], processed=processed, ok_count=summary["success"],
            failed_count=summary["failed"], current_item=symbol,
            message=_progress_message(self.dataset, processed, summary["total"]))
        # Cancellation may suppress the progress update. Already processed
        # source rows and their outcomes must still be durable for retry.
        if task is None or task.status in {"done", "failed", "cancelled"} or task.cancel_requested:
            db.commit()
        self.pending.clear()
        self.written_points.clear()
        self.processed_symbols.clear()
        self.count, self.last, self.updated_at = 0, None, time.monotonic()


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
        symbols = resolve_external_symbols(db, source, asset_type, payload.get("watchlist_id"), payload.get("portfolio_id"))
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
    policy = sync_policy(payload)
    write_options = {"fill_missing_only": True} if policy == "missing" else {}
    local_current = (int(payload.get("_already_current_count", 0)) if dataset == "fundamental" else 0) + int(payload.get("_covered_symbol_count", 0))
    result["total"] += local_current
    result["skipped"] += local_current
    pending_northbound = dataset == "capital_flow" and payload.get("include_northbound", True) and (
        not sync_plan_id or bool(db.scalar(select(DataSyncPartition.id).where(
            DataSyncPartition.plan_id == sync_plan_id,
            DataSyncPartition.partition_key.like("northbound:%"),
            DataSyncPartition.status.in_(("queued", "failed", "cancelled")),
        ).limit(1)))
    )
    if dataset == "fundamental":
        missing_industry = [symbol.symbol for symbol in symbols if not symbol.industry]
        result["field_coverage"] = {"industry": {
            "source": "local_symbol_master", "total": len(symbols),
            "available": len(symbols) - len(missing_industry),
            "missing": len(missing_industry), "missing_sample": missing_industry[:20],
        }}

    def finish_without_fetch(reason: str, message: str) -> dict:
        result.update(skipped=result["total"], skip_reason=reason, message=message)
        for partition in partitions_by_symbol.values():
            partition.status = "skipped"
            partition.rows_written = 0
            partition.error_message = None
            partition.finished_at = partition.updated_at = _now()
        if sync_plan_id:
            # Include non-symbol partitions such as northbound flow.
            db.execute(update(DataSyncPartition).where(
                DataSyncPartition.plan_id == sync_plan_id,
                DataSyncPartition.status.in_(("queued", "failed")),
            ).values(status="skipped", rows_written=0, error_message=None,
                     finished_at=_now(), updated_at=_now()))
        db.commit()
        if sync_plan_id:
            _refresh_sync_plan(db, sync_plan_id, terminal=True)
        return result

    if not symbols and local_current and not pending_northbound:
        return finish_without_fetch("already_current", "所选范围已覆盖，无需重复同步")

    if dataset in {"fundamental", "capital_flow", "etf"} and plan.get("mode") != "backfill":
        from app.services.market_session import is_cn_trading_day

        if not is_cn_trading_day(db, range_end):
            return finish_without_fetch("market_closed", f"{range_end} 休市，无需更新")
    elif dataset in {"fundamental", "capital_flow"}:
        from app.services.market_session import range_is_cn_closed

        if range_is_cn_closed(db, range_start, range_end):
            return finish_without_fetch("market_closed", "所选日期范围均为 A 股休市日，无需更新")

    if dataset == "fundamental" and plan.get("mode") != "backfill" and symbols and policy == "normal":
        from app.services.fundamental_data import complete_daily_valuation_ids

        complete_ids = complete_daily_valuation_ids(db, [symbol.id for symbol in symbols], range_end)
        if len(complete_ids) == len(symbols):
            return finish_without_fetch("already_current", "所选标的估值已是最新，无需更新")
        # Persist local skips and remove them before selecting HTTP batches.
        result["skipped"] += len(complete_ids)
        symbols = [symbol for symbol in symbols if symbol.id not in complete_ids]
        for symbol_id in complete_ids:
            partition = partitions_by_symbol.pop(symbol_id, None)
            if partition is not None:
                partition.status = "skipped"
                partition.rows_written = 0
                partition.finished_at = partition.updated_at = _now()
        db.commit()
    raw_ranges = payload.get("_policy_ranges")
    if isinstance(raw_ranges, dict):
        policy_ranges = {symbol.id: [(_as_date(a), _as_date(b)) for a, b in raw_ranges.get(str(symbol.id), ())]
                         for symbol in symbols}
    else:
        policy_ranges = resolve_policy_ranges(db, dataset, [symbol.id for symbol in symbols],
                                              range_start, range_end, policy)
    for symbol in symbols:
        partition = partitions_by_symbol.get(symbol.id)
        if partition is not None:
            policy_ranges[symbol.id] = [(max(a, partition.start_date), min(b, partition.end_date))
                                       for a, b in policy_ranges.get(symbol.id, ())
                                       if a <= partition.end_date and b >= partition.start_date]
    # Gap-repair requests freeze exact selected dates instead of widening a
    # sparse selection to every date between its minimum and maximum.
    selected = payload.get("_selected_gap_dates") or {}
    for symbol in symbols:
        if str(symbol.id) in selected:
            wanted = {_as_date(value) for value in selected[str(symbol.id)]}
            available = {day for a, b in policy_ranges[symbol.id] for day in requested_days(db, dataset, a, b)}
            days = requested_days(db, dataset, range_start, range_end)
            policy_ranges[symbol.id] = subtract_ranges(days, set(days) - (wanted & available))
    inactive = {symbol.id for symbol in symbols if not policy_ranges.get(symbol.id)}
    for sid in inactive:
        partition = partitions_by_symbol.pop(sid, None)
        if partition is not None:
            partition.status, partition.rows_written = "skipped", 0
            partition.finished_at = partition.updated_at = _now()
    result["skipped"] += len(inactive)
    symbols = [symbol for symbol in symbols if symbol.id not in inactive]
    if inactive:
        db.commit()
    if not symbols and result["skipped"] and not pending_northbound:
        return finish_without_fetch("already_current", "所选范围已覆盖，无需重复同步")
    _update_task(
        db,
        task_id,
        status="running",
        stage="prepare",
        percent=2,
        total=result["total"],
        message=f"Resolved {len(symbols)} symbols",
        started_at=_now(),
    )

    northbound_partition = None
    if sync_plan_id and dataset == "capital_flow":
        northbound_partition = db.execute(select(DataSyncPartition).where(DataSyncPartition.plan_id == sync_plan_id, DataSyncPartition.partition_key.like("northbound:%"), DataSyncPartition.status.in_(("queued", "failed", "cancelled")))).scalars().first()
    if dataset == "capital_flow" and payload.get("include_northbound", True) and (not sync_plan_id or northbound_partition is not None):
        from app.services.capital_flow_data import sync_northbound_flow

        _update_task(db, task_id, stage="northbound", percent=4, message="Syncing northbound flow")
        if _is_cancelled(db, task_id):
            return None
        result["total"] += 1
        _set_partition_status(db, northbound_partition, "running")
        try:
            northbound_start = range_start if plan.get("mode") == "backfill" else None
            northbound_days = min(max((range_end - range_start).days + 1, 1), 100)
            count = sync_northbound_flow(
                db, days=northbound_days, start_date=northbound_start, end_date=range_end, **write_options
            )
            result["records"] += count
            result["success" if count else "skipped"] += 1
            result["northbound"] = {"status": "done", "records": count, "start_date": range_start.isoformat(), "end_date": range_end.isoformat()}
            _set_partition_status(db, northbound_partition, "done" if count else "skipped", rows_written=count)
            if count:
                stmt = select(NorthboundFlow.trade_date).where(NorthboundFlow.trade_date <= range_end)
                if northbound_start is not None:
                    stmt = stmt.where(NorthboundFlow.trade_date >= northbound_start)
                dates = db.execute(stmt.order_by(desc(NorthboundFlow.trade_date)).limit(northbound_days)).scalars().all()
                _merge_written_dates(result, dates)
                result["northbound"].update(start_date=min(dates).isoformat(), end_date=max(dates).isoformat())
            db.commit()
        except Exception as exc:
            db.rollback()
            result["failed"] += 1
            result["northbound"] = {"status": "warning", "records": 0, "error": str(exc)}
            _set_partition_status(db, northbound_partition, "failed", error_message=str(exc))
            result["errors"].append(f"northbound: {exc}")
            logger.warning("northbound sync failed in external task: %s", exc)

    if dataset == "financial" and plan.get("mode") != "backfill":
        from app.services.financial_data import sync_financial_announcements
        from app.services.sync_progress import CancellationProbe
        if not _wait_for_market_priority(db, task_id):
            return None

        checkpoint = _SymbolBatchCheckpoint(db, task_id, dataset, partitions_by_symbol, policy_ranges)
        previously_skipped = result["skipped"]
        try:
            result.update(sync_financial_announcements(db, symbols, range_end, range_end,
                should_cancel=CancellationProbe(lambda: _is_cancelled(db, task_id)),
                on_result=checkpoint, **write_options))
        finally:
            checkpoint.flush()
        result["total"] += previously_skipped
        result["skipped"] += previously_skipped
        db.commit()
        if sync_plan_id:
            _refresh_sync_plan(db, sync_plan_id, terminal=True)
        return result

    # Daily valuation and fund flow use selected code/date batches. Failures
    # never switch to individual history downloads.
    if dataset in {"fundamental", "capital_flow"} and plan.get("mode") != "backfill":
        # Only a residual top-up (already-current symbols were removed above)
        # may treat an empty snapshot as "no new data"; a full unresolved
        # request still surfaces an unpublished provider snapshot.
        residual_topup = len(symbols) < int(result.get("total") or 0)
        if dataset == "capital_flow":
            from app.services.capital_flow_data import (
                sync_market_capital_flow_snapshot,
                sync_symbol_capital_flow,
            )
            from app.services.capital_flow_provider import BATCH_SIZE as _snapshot_batch_size

            def _snapshot_sync(active_db: Session) -> set[int]:
                return sync_market_capital_flow_snapshot(active_db, symbols, range_end,
                    should_cancel=lambda: _is_cancelled(active_db, task_id), **write_options)

            def _single_symbol_sync(active_db: Session, symbol: Symbol) -> Any:
                return sync_symbol_capital_flow(active_db, symbol, range_end)
        else:
            from app.services.fundamental_data import (
                sync_market_valuation_snapshot,
            )
            from app.services.valuation_provider import DAILY_BATCH_SIZE as _snapshot_batch_size

            def _snapshot_sync(active_db: Session) -> set[int]:
                return sync_market_valuation_snapshot(active_db, symbols, range_end,
                    should_cancel=lambda: _is_cancelled(active_db, task_id),
                    allow_empty_snapshot=residual_topup, **write_options)

        if _is_cancelled(db, task_id):
            return None
        try:
            synced_ids = _snapshot_sync(db) or set()
        except Exception as exc:
            db.rollback()
            if dataset == "fundamental":
                if _is_cancelled(db, task_id):
                    return None
                raise RuntimeError(f"当日估值同步已停止，未切换历史接口：{exc}") from exc
            if _is_cancelled(db, task_id):
                return None
            raise RuntimeError(f"资金流当日接口不可用，已停止同步，未执行逐标的兜底或历史下载：{exc}") from exc
        result["success"] += len(synced_ids)
        result["skipped"] += max(len(symbols) - len(synced_ids), 0)
        result["records"] += len(synced_ids)
        if not synced_ids and (
            residual_topup
            or (dataset == "fundamental" and len(symbols) < _snapshot_batch_size)
        ):
            # A small tail top-up (for example the last 9 symbols) has no
            # first-batch evidence; an empty snapshot means "nothing new",
            # not a failed whole-market sync.
            result["skip_reason"] = "no_new_data"
            result["message"] = "所选标的当日快照尚未发布或无新增数据，已跳过"
        _merge_written_dates(result, getattr(synced_ids, "trade_dates", ()))
        for symbol_id, partition in partitions_by_symbol.items():
            partition.status = "done" if symbol_id in synced_ids else "skipped"
            partition.attempts = max(partition.attempts, 1)
            partition.rows_written = 1 if symbol_id in synced_ids else 0
            partition.finished_at = _now()
            partition.updated_at = _now()
        if synced_ids:
            _record_source_points(db, dataset, list(synced_ids), range_end, range_end, task_id)
        db.commit()
        if _is_cancelled(db, task_id):
            return None
        _update_task(
            db,
            task_id,
            status="running",
            stage="sync",
            percent=99,
            processed=result["success"] + result["skipped"] + result["failed"],
            ok_count=result["success"],
            failed_count=result["failed"],
            current_item=None,
            message=_progress_message(dataset, len(symbols), len(symbols)),
        )
        if sync_plan_id:
            _refresh_sync_plan(db, sync_plan_id, terminal=True)
        return result

    # Historical valuation requests are independent per symbol. Run them in
    # bounded batches with one SQLAlchemy session per worker; the coordinator
    # remains responsible for task progress, partition state and cancellation.
    if dataset == "fundamental" and plan.get("mode") == "backfill" and symbols:
        from app.services.fundamental_data import sync_symbol_valuation_range

        max_workers = min(max(int(payload.get("max_workers", 4)), 1), 8)

        def _backfill_worker(symbol_id: int, start: date, end: date):
            worker_db = SessionLocal()
            try:
                worker_symbol = worker_db.get(Symbol, symbol_id)
                if worker_symbol is None:
                    return symbol_id, 0, "symbol not found", ()
                count, dates = 0, set()
                for begin, finish in policy_ranges.get(symbol_id, [(start, end)]):
                    if _is_cancelled(worker_db, task_id):
                        raise InterruptedError("同步已取消")
                    allowed = {_as_date(day) for day in selected[str(symbol_id)]} if str(symbol_id) in selected else None
                    options = {**write_options, **({"allowed_dates": allowed} if allowed is not None else {})}
                    written = sync_symbol_valuation_range(worker_db, worker_symbol,
                        start_date=begin, end_date=finish, quiet_output=False, **options)
                    count += int(written)
                    dates.update(getattr(written, "sync_written_dates", ()))
                if dates:
                    _record_source_points(worker_db, dataset, [symbol_id], start, end, task_id,
                                          allowed={symbol_id: dates})
                worker_db.commit()
                return symbol_id, count, None, tuple(dates)
            except Exception as exc:
                worker_db.rollback()
                return symbol_id, 0, str(exc), ()
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
                        _, count, error, dates = future.result()
                    except Exception as exc:  # defensive: worker normally returns errors
                        count, error, dates = 0, str(exc), ()
                    processed += 1
                    if error:
                        result["failed"] += 1
                        if len(result["errors"]) < 20:
                            result["errors"].append(f"{symbol.symbol}: {error}")
                        _set_partition_status(db, partition, "failed", error_message=error)
                    elif count > 0:
                        result["success"] += 1
                        result["records"] += count
                        _merge_written_dates(result, dates)
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

    if dataset == "etf":
        from app.services.etf_basic_data import sync_etf_indicator_range, sync_etf_snapshots
        from app.services.sync_progress import CancellationProbe

        if plan.get("mode") == "backfill":
            if not _wait_for_market_priority(db, task_id):
                return None
            policy = sync_policy(payload)
            for index, symbol in enumerate(symbols):
                if _is_cancelled(db, task_id):
                    return None
                partition = partitions_by_symbol.get(symbol.id)
                part_start = partition.start_date if partition is not None else range_start
                part_end = partition.end_date if partition is not None else range_end
                segments = _etf_partial_history_ranges(db, symbol.id, part_start, part_end, policy)
                written = 0
                dates: set[date] = set()
                failed = False
                for begin, finish in segments:
                    try:
                        summary = sync_etf_indicator_range(
                            db, symbol, begin, finish,
                            should_cancel=lambda: _is_cancelled(db, task_id),
                        )
                    except InterruptedError:
                        db.rollback()
                        return None
                    except Exception as exc:
                        db.rollback()
                        failed = True
                        result["failed"] += 1
                        if len(result["errors"]) < 20:
                            result["errors"].append(f"{symbol.symbol}: {exc}")
                        logger.warning(
                            "ETF history backfill %s %s..%s failed: %s",
                            symbol.symbol, begin, finish, exc,
                        )
                        break
                    written += int(summary.get("written") or 0)
                    dates.update(summary.get("written_dates") or ())
                    db.commit()
                if failed:
                    _set_partition_status(db, partition, "failed",
                                          error_message="；".join(result["errors"][-3:]))
                elif written:
                    result["success"] += 1
                    result["records"] += written
                    _merge_written_dates(result, dates)
                    _record_source_points(db, dataset, [symbol.id], part_start, part_end,
                                          task_id, allowed={symbol.id: dates})
                    db.commit()
                    _set_partition_status(db, partition, "done", rows_written=written)
                else:
                    result["skipped"] += 1
                    _set_partition_status(db, partition, "skipped")
                _update_task(
                    db, task_id,
                    status="running", stage="sync",
                    percent=100 if not symbols else min(99, 5 + 94 * (index + 1) / len(symbols)),
                    total=len(symbols), processed=index + 1,
                    ok_count=result["success"], failed_count=result["failed"],
                    current_item=symbol.symbol,
                    message=f"ETF 历史回填 {index + 1}/{len(symbols)}",
                )
            if sync_plan_id:
                _refresh_sync_plan(db, sync_plan_id, terminal=True)
            return result

        if not _wait_for_market_priority(db, task_id):
            return None

        checkpoint = _SymbolBatchCheckpoint(db, task_id, dataset, partitions_by_symbol)
        previously_skipped = result["skipped"]
        try:
            result.update(sync_etf_snapshots(db, symbols, range_end,
                should_cancel=CancellationProbe(lambda: _is_cancelled(db, task_id)),
                on_result=checkpoint, **write_options))
        finally:
            checkpoint.flush()
        result["total"] += previously_skipped
        result["skipped"] += previously_skipped
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
            actual_dates = set()
            segments = policy_ranges.get(symbol.id, [(partition_start, partition_end)])
            if dataset == "fundamental":
                from app.services.fundamental_data import sync_symbol_valuation

                if plan.get("mode") == "backfill":
                    from app.services.fundamental_data import sync_symbol_valuation_range

                    count = 0
                    for begin, finish in segments:
                        written = sync_symbol_valuation_range(db, symbol, start_date=begin, end_date=finish, **write_options)
                        count += int(written)
                        actual_dates.update(getattr(written, "sync_written_dates", ()))
                else:
                    item = sync_symbol_valuation(db, symbol, range_end)
                    count = 1 if item is not None else 0
            elif dataset == "financial":
                from app.services.financial_data import sync_symbol_financial_reports

                if plan.get("mode") == "backfill":
                    count = 0
                    for begin, finish in segments:
                        if _is_cancelled(db, task_id):
                            raise InterruptedError("财报同步已取消")
                        written = sync_symbol_financial_reports(db, symbol,
                            announcement_start=begin, announcement_end=finish, **write_options)
                        count += int(written)
                        actual_dates.update(getattr(written, "sync_written_dates", ()))
                    _record_financial_scan(db, [symbol], {symbol.id: segments}, task_id)
                else:
                    count = sync_symbol_financial_reports(db, symbol, announcement_end=range_end, incremental=True)
            elif dataset == "capital_flow":
                from app.services.capital_flow_data import sync_symbol_capital_flow

                if plan.get("mode") == "backfill":
                    from app.services.capital_flow_data import sync_symbol_capital_flow_range

                    allowed = {day for begin, finish in segments for day in requested_days(db, dataset, begin, finish)}
                    if str(symbol.id) in selected:
                        allowed &= {_as_date(day) for day in selected[str(symbol.id)]}
                    count = sync_symbol_capital_flow_range(db, symbol,
                        start_date=min(allowed), end_date=max(allowed), allowed_dates=allowed, **write_options)
                    actual_dates.update(getattr(count, "sync_written_dates", ()))
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
                if plan.get("mode") == "backfill":
                    dates = actual_dates
                elif dataset == "financial":
                    dates = _local_written_dates(db, dataset, symbol.id, end=range_end)
                else:
                    dates = getattr(item, "sync_written_dates", ()) or (getattr(item, "trade_date", None),)
                _merge_written_dates(result, dates)
                if actual_dates:
                    _record_source_points(db, dataset, [symbol.id], partition_start, partition_end, task_id,
                                          allowed={symbol.id: actual_dates})
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
    if dataset == "tail_proxy":
        reject_tail_proxy()
    from app.services.market_session import is_cn_trading_day, range_is_cn_closed

    plan = payload.get("plan") or build_external_sync_plan(dataset, payload)
    start = _as_date(plan["requested_start_date"])
    end = _as_date(plan["requested_end_date"])
    policy = sync_policy(payload)
    write_options = {"fill_missing_only": True} if policy == "missing" else {}
    closed = (
        range_is_cn_closed(db, start, end) if plan.get("mode") == "backfill"
        else not is_cn_trading_day(db, end)
    )
    if closed:
        plan_id = payload.get("sync_plan_id")
        count = 0
        if plan_id:
            count = db.execute(update(DataSyncPartition).where(
                DataSyncPartition.plan_id == plan_id,
                DataSyncPartition.status.in_(("queued", "failed")),
            ).values(status="skipped", rows_written=0, finished_at=_now(), updated_at=_now())).rowcount
            db.commit()
            _refresh_sync_plan(db, plan_id, terminal=True)
        return {"dataset": dataset, "total": count, "success": 0, "skipped": count,
                "failed": 0, "records": 0, "errors": [], "skip_reason": "market_closed",
                "message": "所选日期范围均为 A 股休市日，无需更新"}
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
    segments = resolve_policy_ranges(db, dataset, [0], start, end, policy)[0]
    if not segments:
        _set_partition_status(db, partition, "skipped")
        if sync_plan_id:
            _refresh_sync_plan(db, sync_plan_id, terminal=True)
        return {"dataset": dataset, "total": 0, "success": 0, "skipped": 0,
                "failed": 0, "records": 0, "errors": [], "skip_reason": "already_current",
                "message": "所选范围已确认覆盖，无需重复同步"}
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
            from app.services.sync_coverage import record_coverage_points

            result = {"dataset": dataset, "total": 0, "success": 0, "skipped": 0,
                      "failed": 0, "records": 0, "errors": []}
            chunk_days = max(int(plan.get("chunk_days") or 31), 1)
            chunks: list[tuple[date, date]] = []
            for begin, finish in segments:
                cursor = begin
                while cursor <= finish:
                    chunk_end = min(finish, cursor + timedelta(days=chunk_days - 1))
                    chunks.append((cursor, chunk_end))
                    cursor = chunk_end + timedelta(days=1)
            planned_days = max(sum((finish - begin).days + 1 for begin, finish in chunks), 1)
            processed_days = 0
            summary = None
            for begin, finish in chunks:
                if _is_cancelled(db, task_id):
                    raise InterruptedError("龙虎榜同步已取消")
                try:
                    summary = sync_lhb_institution_trades(db, start_date=begin, end_date=finish, **write_options)
                    result["total"] += summary.received
                    result["success"] += summary.written
                    result["records"] += summary.written
                    result["skipped"] += summary.unmatched
                    _merge_written_dates(result, getattr(summary, "written_dates", ()))
                    # Persist each completed window before continuing so a
                    # later provider failure cannot roll back earlier history.
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    result["failed"] += 1
                    if len(result["errors"]) < 20:
                        result["errors"].append(
                            f"{begin.isoformat()} ~ {finish.isoformat()}: {type(exc).__name__}: {exc}"
                        )
                    logger.warning("LHB chunk %s..%s failed: %s", begin, finish, exc)
                else:
                    # A completed past event-list scan also proves genuine empty
                    # days. Unmatched records must remain repairable after import.
                    if not summary.unmatched:
                        days = [day for day in requested_days(db, dataset, begin, finish) if day < date.today()]
                        try:
                            record_coverage_points(db, dataset, {0: days}, task_id=task_id, complete=True)
                            db.commit()
                        except Exception as exc:
                            db.rollback()
                            logger.warning("LHB coverage record %s..%s failed: %s", begin, finish, exc)
                processed_days += (finish - begin).days + 1
                _update_task(
                    db,
                    task_id,
                    status="running",
                    stage="fetch",
                    percent=min(90, 10 + int(80 * processed_days / planned_days)),
                    total=result["total"],
                    processed=result["success"] + result["skipped"] + result["failed"],
                    ok_count=result["success"],
                    failed_count=result["failed"],
                    current_item=f"{begin.isoformat()} ~ {finish.isoformat()}",
                    message=f"龙虎榜分段同步 {processed_days}/{planned_days} 天",
                )
        elif dataset == "hot_rank":
            from app.services.hot_rank_data import sync_hot_rank_snapshot

            summary = sync_hot_rank_snapshot(db, **write_options)
            result = {
                "dataset": dataset, "total": summary.received,
                "success": summary.written, "skipped": summary.unmatched,
                "failed": 0, "records": summary.written, "errors": [],
            }
        else:
            from app.services.tail_proxy_data import sync_tail_proxy_snapshots

            scope = {key: value for key, value in {"watchlist_id": payload.get("watchlist_id"), "portfolio_id": payload.get("portfolio_id")}.items() if value is not None}
            if sync_plan_id:
                scope["should_cancel"] = lambda: _is_cancelled(db, task_id)
            summary = sync_tail_proxy_snapshots(
                db, source=str(payload.get("source") or "candidates"),
                limit=int(payload.get("limit", 20)), **scope,
            )
            result = {
                "dataset": dataset, "total": summary.total,
                "success": summary.written, "skipped": summary.skipped,
                "failed": summary.failed, "records": summary.written,
                "errors": list(summary.errors),
            }
        if result["records"]:
            if dataset == "lhb":
                dates = getattr(summary, "written_dates", ())
            else:
                dates = (getattr(summary, "trade_date", None),)
            _merge_written_dates(result, dates)
            if dataset in {"hot_rank", "lhb"}:
                from app.services.sync_coverage import record_coverage_points
                # Popularity is a current snapshot, with no provider closing
                # timestamp proving a day's final ranking.
                record_coverage_points(db, dataset, {0: [day for day in dates if day is not None]},
                                       task_id=task_id, complete=False)
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
        message="本地镜像写入中（数据量较大，请等待心跳更新）",
    )
    try:
        from app.services.factors.data_sync import mirror_factor_inputs

        # 镜像可能需要处理数十万条本地记录，批次之间没有网络请求可供
        # 外层任务自然刷新心跳。通过取消回调复用批处理边界，并节流写回
        # 任务时间戳，避免巡检误判为停滞。
        last_heartbeat = time.monotonic()

        def mirror_checkpoint() -> bool:
            nonlocal last_heartbeat
            if _is_cancelled(db, task_id):
                return True
            now = time.monotonic()
            if now - last_heartbeat >= 5:
                _update_task(
                    db,
                    task_id,
                    stage="mirror",
                    percent=99,
                    message="本地镜像写入中（仍在处理，请勿重复启动）",
                )
                last_heartbeat = now
            return False

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
            should_cancel=mirror_checkpoint,
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


def finalize_legacy_external_sync(db, task_id, dataset, records, dates, *, skip_reason=None):
    """Older scheduled/discovery workers publish through the same warehouse path."""
    if not records:
        reason = skip_reason or "no_records_written"
        return {"warehouse_mirror": {"status": "skipped", "records": 0, "reason": reason},
                "quality_snapshot": {"status": "skipped", "reason": reason}}
    actual_dates = [day for day in dates if isinstance(day, date)]
    if not actual_dates:
        raise ValueError("外部同步缺少实际写入日期，未执行仓库镜像")
    plan = {"requested_start_date": min(actual_dates).isoformat(), "requested_end_date": max(actual_dates).isoformat()}
    mirror = _mirror_external_factor_inputs(db, task_id, dataset, plan)
    if dataset in {"lhb", "hot_rank"}:
        # Event/snapshot lists never prove a whole-market complete day, so
        # record a dataset-level partial point for the dates actually written.
        from app.services.sync_coverage import record_coverage_points
        try:
            record_coverage_points(db, dataset, {0: actual_dates}, task_id=task_id, complete=False)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("legacy %s coverage record failed for task %s: %s", dataset, task_id, exc)
    from app.services.data_quality import capture_field_quality_snapshots
    try:
        _update_task(db, task_id, stage="quality", percent=99, message="正在检查本次数据集的字段质量")
        quality = capture_field_quality_snapshots(db, trigger=f"external_sync:{dataset}", dataset=dataset)
    except Exception as exc:
        quality = {"status": "warning", "error": str(exc)}
    return {"warehouse_mirror": mirror, "quality_snapshot": quality}


def _run_external_sync_task(task_id: str, dataset: ExternalDataset, payload: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        if dataset == "tail_proxy":
            reject_tail_proxy()
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status in {"done", "failed", "cancelled"} or task.is_terminal_locked:
            return
        if task.started_at is None:
            task.started_at = _now()
            db.commit()
        if payload.get("sync_plan_id"):
            _refresh_sync_plan(db, payload["sync_plan_id"])
        if payload.get("_preflight_skip"):
            result = {"dataset": dataset, "total": 0, "success": 0, "skipped": 0,
                      "failed": 0, "records": 0, "errors": [],
                      "skip_reason": "market_closed", "message": payload["_preflight_skip"]}
            if payload.get("sync_plan_id"):
                _refresh_sync_plan(db, payload["sync_plan_id"], terminal=True)
        elif payload.get("_mirror_only"):
            result = {"dataset": dataset, "total": 0, "success": 0, "skipped": 0, "failed": 0, "records": 0, "errors": [], "mirror_retry": True}
            result.update(payload.get("_mirror_written_range") or {})
        elif dataset in ("fundamental", "financial", "capital_flow", "etf"):
            result = _run_symbol_sync(db, task_id, dataset, payload)
        else:
            result = _run_bulk_sync(db, task_id, dataset, payload)
        if result is None or _is_cancelled(db, task_id):
            _mark_cancelled(db, task_id)
            return
        if not result.get("records") and not payload.get("_mirror_only"):
            if not result.get("failed") and not result.get("skip_reason"):
                reason = "no_events" if dataset == "lhb" else "no_new_data" if dataset == "financial" else "data_unavailable"
                message = {"no_events": "所选区间没有龙虎榜机构事件，无新增数据", "no_new_data": "所选公告日期没有新增或变更财报"}.get(reason, "接口未返回所选日期的有效数据，未写入记录")
                result.update(skip_reason=reason, message=message)
        plan = payload.get("plan") or build_external_sync_plan(dataset, payload)
        result["plan"] = plan
        if _is_cancelled(db, task_id):
            _mark_cancelled(db, task_id)
            return
        mirror_plan = dict(plan)
        if result.get("written_start_date") and result.get("written_end_date"):
            mirror_plan["requested_start_date"] = result["written_start_date"]
            mirror_plan["requested_end_date"] = result["written_end_date"]
        no_writes = not result.get("records") and not payload.get("_mirror_only")
        mirror = (
            {"status": "skipped", "records": 0, "reason": result.get("skip_reason", "no_records_written")}
            if result.get("skip_reason") or no_writes
            else _mirror_external_factor_inputs(db, task_id, dataset, mirror_plan)
        )
        mirror["start_date"] = mirror_plan.get("requested_start_date")
        mirror["end_date"] = mirror_plan.get("requested_end_date")
        result["warehouse_mirror"] = mirror
        if mirror.get("status") == "warning":
            result["errors"].append(f"warehouse_mirror: {mirror['error']}")
        if _is_cancelled(db, task_id):
            _mark_cancelled(db, task_id)
            return
        try:
            from app.services.data_quality import capture_field_quality_snapshots

            if not result.get("skip_reason") and not no_writes:
                _update_task(db, task_id, stage="quality", percent=99, message="正在检查本次数据集的字段质量")
            result["quality_snapshot"] = (
                {"status": "skipped", "reason": result.get("skip_reason", "no_records_written")}
                if result.get("skip_reason") or no_writes
                else capture_field_quality_snapshots(db, trigger=f"external_sync:{dataset}", dataset=dataset)
            )
        except Exception as exc:
            logger.warning("quality snapshot failed after external task %s: %s", task_id, exc)
            result["quality_snapshot"] = {"status": "warning", "error": str(exc)}
            result["errors"].append(f"quality_snapshot: {exc}")
        processed = result["success"] + result["skipped"] + result["failed"]
        if _is_cancelled(db, task_id):
            _mark_cancelled(db, task_id)
            return
        retryable_failure = result["failed"] > 0 or mirror.get("status") == "warning"
        if payload.get("_mirror_only") and payload.get("sync_plan_id"):
            plan_record = db.get(DataSyncPlan, payload["sync_plan_id"])
            if plan_record is not None:
                plan_record.status = "failed" if retryable_failure else "done"
                plan_record.finished_at = _now()
                db.commit()
        _update_task(
            db,
            task_id,
            status="failed" if retryable_failure else "done",
            stage="failed" if retryable_failure else "done",
            percent=100,
            total=result["total"],
            processed=processed,
            ok_count=result["success"],
            failed_count=result["failed"],
            current_item=None,
            message=result.get("message") or (
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
        if _is_cancelled(db, task_id):
            if payload.get("sync_plan_id"):
                _refresh_sync_plan(db, payload["sync_plan_id"], terminal=True)
            _mark_cancelled(db, task_id)
            return
        logger.exception("external data task %s failed", task_id)
        sync_plan_id = str(payload.get("sync_plan_id") or "")
        if sync_plan_id:
            plan_record = db.get(DataSyncPlan, sync_plan_id)
            if plan_record is not None:
                running_partitions = db.execute(
                    select(DataSyncPartition).where(
                        DataSyncPartition.plan_id == sync_plan_id,
                        DataSyncPartition.status.in_(("queued", "running")),
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
            task.is_terminal_locked = 1
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


def _await_market_preflight(db: Session, dataset: ExternalDataset) -> None:
    """Bounded read-only wait before acquiring creation ownership."""
    for _ in range(24):
        market_task = _active_market_priority_task(db)
        if market_task is None:
            return
        time.sleep(_MARKET_PRIORITY_WAIT_SECONDS)
        db.rollback()  # refresh a repeatable-read snapshot; no writes in preflight
    market_task = _active_market_priority_task(db)
    if market_task is not None:
        raise RuntimeError(
            f"【行情同步优先级】行情任务 {market_task.task_type} 仍在运行"
            f"（{market_task.processed}/{market_task.total}，{market_task.percent:.1f}%）；"
            "请等待行情完成后重试。"
        )


def start_external_data_sync(dataset: ExternalDataset, payload: dict[str, Any]) -> AsyncTaskRead:
    if dataset == "tail_proxy":
        reject_tail_proxy()
    if dataset not in EXTERNAL_DATASETS:
        raise ValueError(f"Unsupported external dataset: {dataset}")
    payload = dict(payload)
    payload.pop("_preflight_skip", None)
    payload.pop("_already_current_count", None)
    payload.pop("_covered_symbol_count", None)
    payload.pop("_policy_ranges", None)
    plan = build_external_sync_plan(dataset, payload)
    parent_plan_id = payload.pop("_parent_plan_id", None)
    partition_specs = payload.pop("_partition_specs", None)
    task_payload = {"dataset": dataset, **payload, "plan": plan}
    db = SessionLocal()
    try:
        # Detect closed dates before symbol resolution and partition creation.
        # A no-op keeps one audit task/plan, never thousands of symbol rows.
        closed = False
        if dataset != "financial" and not task_payload.get("_mirror_only"):
            from app.services.market_session import is_cn_trading_day, range_is_cn_closed

            try:
                start, end = _as_date(plan["requested_start_date"]), _as_date(plan["requested_end_date"])
                closed = (range_is_cn_closed(db, start, end) if plan["mode"] == "backfill"
                          else not is_cn_trading_day(db, end))
            except RuntimeError:
                # Unknown calendar is handled as a terminal diagnostic by
                # the worker, without contacting market-data providers.
                closed = False
            if closed:
                task_payload["_preflight_skip"] = "所选日期为 A 股休市日，无需更新"
                partition_specs = []
        # Wait before obtaining database ownership: provider priority must not
        # hold SQLite's writer reservation or a MySQL named lock for 120s.
        if not closed:
            _await_market_preflight(db, dataset)
        db.rollback()  # preflight is read-only; create starts its own transaction
        with _external_sync_creation_guard(db, dataset):
            existing = db.execute(
                select(AsyncTaskRecord)
                .where(
                    AsyncTaskRecord.task_type == _TASK_TYPES[dataset],
                    AsyncTaskRecord.status.in_(("queued", "running")),
                )
                .order_by(desc(AsyncTaskRecord.created_at))
            ).scalars().first()
            if existing is not None:
                raise RuntimeError("已有同类型外部数据同步任务在运行，请稍后再试")
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
    try:
        worker.start()
    except Exception as exc:
        failure_db = SessionLocal()
        try:
            _update_task(failure_db, task_id, status="failed", stage="failed", percent=100, failed_count=1, message=f"Worker could not start: {exc}", errors_json=json.dumps([{"stage": "dispatch", "error": str(exc)}], ensure_ascii=False), finished_at=_now())
            persisted = failure_db.get(DataSyncPlan, task_payload["sync_plan_id"])
            if persisted is not None:
                persisted.status = "failed"
                persisted.finished_at = _now()
                failure_db.commit()
        finally:
            failure_db.close()
        raise RuntimeError("外部同步工作线程启动失败；任务已保留，可重试") from exc
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
                "sync_policy": plan_record.sync_policy,
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
    if cancelled.status in {"done", "failed"}:
        return cancelled
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
        prior_result = {}
        try:
            prior_result = json.loads(task.result_json or "{}")
        except json.JSONDecodeError:
            pass
        # Older workers marked partial/provider/mirror failures as done.
        legacy_failure = task.status == "done" and (int(task.failed_count or 0) > 0 or prior_result.get("warehouse_mirror", {}).get("status") == "warning")
        if task.status not in ("failed", "cancelled") and not legacy_failure:
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
        elif original_plan is not None and prior_result.get("warehouse_mirror", {}).get("status") == "warning":
            # Provider rows are already durable. Retry only their local mirror,
            # preserving the original publication / collection evidence.
            payload["_parent_plan_id"] = original_plan.id
            payload["_partition_specs"] = []
            payload["_mirror_only"] = True
            payload["_mirror_written_range"] = {key: prior_result[key] for key in ("written_start_date", "written_end_date") if key in prior_result}
        elif isinstance(recovery, dict) and recovery.get("last_symbol_id") is not None:
            # Compatibility fallback for tasks created before partition persistence.
            payload["resume_after_symbol_id"] = recovery["last_symbol_id"]
        frozen_plan = payload.get("plan")
        if isinstance(frozen_plan, dict):
            payload["start_date"] = frozen_plan.get("requested_start_date")
            payload["end_date"] = frozen_plan.get("requested_end_date")
            payload["lookback_days"] = frozen_plan.get("requested_span_days")
        dataset = _DATASET_BY_TASK_TYPE[task.task_type]
        if dataset in {"fundamental", "capital_flow"} and payload.get("mode", "incremental") == "incremental":
            frozen_end = _as_date(payload.get("end_date"))
            if frozen_end is not None and frozen_end != date.today():
                # Yesterday's failed snapshot cannot be reconstructed from
                # today's quote/ranking. Retry its date using real history.
                payload["mode"] = "backfill"
                payload["start_date"] = frozen_end.isoformat()
                payload["end_date"] = frozen_end.isoformat()
                payload["lookback_days"] = 1
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
