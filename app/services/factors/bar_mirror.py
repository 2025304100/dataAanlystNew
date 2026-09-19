"""Incrementally mirror SQLAlchemy daily bars into the factor warehouse."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.factors.batch_audit import begin_batch, finalize_batch
from app.services.factors.store import FactorWarehouse


logger = logging.getLogger(__name__)
BUSINESS_SOURCE_KEY = "sql.daily_bars"
UNIVERSE_SOURCE_KEY = "sql.universe_daily_bars"
UNIVERSE_METADATA_SOURCE_KEY = "sql.universe_symbols"
CancelCheck = Callable[[], bool]
# 进度回调签名：(phase, processed, total, message)
# phase ∈ {"metadata", "universe_bars", "business_bars"}
ProgressCallback = Callable[[str, int, int, str], None]
T = TypeVar("T")


@dataclass
class BarMirrorResult:
    batch_id: str
    business_rows: int = 0
    universe_rows: int = 0
    business_watermark: int = 0
    universe_watermark: int = 0
    metadata_rows: int = 0
    metadata_watermark: int = 0

    @property
    def rows_written(self) -> int:
        return self.business_rows + self.universe_rows

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rows_written"] = self.rows_written
        return payload


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalized_symbol(value: str) -> str:
    return (value or "").strip().upper()


def _cancelled(should_cancel: CancelCheck | None) -> bool:
    return bool(should_cancel and should_cancel())


def _date_filters(column, start_date: date | None, end_date: date | None):
    filters = []
    if start_date is not None:
        filters.append(column >= start_date)
    if end_date is not None:
        filters.append(column <= end_date)
    return filters


def _read_with_retry(
    db: Session,
    operation: Callable[[Session], T],
    *,
    attempts: int = 3,
) -> T:
    """Run one read in a short session and retry a dropped DB connection.

    ORM rows are fully materialized before the short session closes, so a
    subsequent DuckDB write never keeps the MySQL connection checked out.
    Retrying is safe because callers only issue SELECT statements.
    """
    factory = sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
    for attempt in range(1, attempts + 1):
        read_db = factory()
        try:
            return operation(read_db)
        except DBAPIError as exc:
            try:
                read_db.invalidate()
            except Exception:
                pass
            if attempt >= attempts or not _is_disconnect_error(exc):
                raise
            delay = 0.5 * (2 ** (attempt - 1))
            logger.warning(
                "Database connection dropped during factor mirror read; "
                "retrying with a fresh connection in %.1fs (%s/%s)",
                delay,
                attempt,
                attempts,
            )
            time.sleep(delay)
        finally:
            read_db.close()
    raise RuntimeError("unreachable")


def _is_disconnect_error(exc: DBAPIError) -> bool:
    """Return whether a DBAPI error is safe to retry as a read disconnect."""
    if exc.connection_invalidated:
        return True
    values = getattr(exc.orig, "args", ())
    codes = {value for value in values if isinstance(value, int)}
    if codes.intersection({2006, 2013, 10053, 10054}):
        return True
    message = " ".join(str(value) for value in values).lower()
    return any(
        marker in message
        for marker in (
            "server has gone away",
            "lost connection",
            "connection was aborted",
            "connection reset",
        )
    )


def _read_all(db: Session, statement) -> list[Any]:
    return _read_with_retry(db, lambda read_db: read_db.execute(statement).all())


def _read_scalars_all(db: Session, statement) -> list[Any]:
    return _read_with_retry(
        db, lambda read_db: read_db.execute(statement).scalars().all()
    )


def _read_scalar(db: Session, statement) -> Any:
    return _read_with_retry(db, lambda read_db: read_db.execute(statement).scalar())


def _watermark_key(
    base_key: str, start_date: date | None, end_date: date | None
) -> str:
    if start_date is None and end_date is None:
        return base_key
    return (
        f"{base_key}|{start_date.isoformat() if start_date else ''}|"
        f"{end_date.isoformat() if end_date else ''}"
    )


def _business_record(
    bar: DailyBar,
    symbol: Symbol,
    *,
    batch_id: str,
    adjust: str,
    ingested_at: datetime,
) -> dict[str, Any]:
    return {
        "symbol": _normalized_symbol(symbol.symbol),
        "trade_date": bar.trade_date,
        "adjust": adjust,
        "business_symbol_id": symbol.id,
        "universe_symbol_id": None,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": bar.amount,
        "turnover_rate": bar.turnover_rate,
        "source": bar.source or "unknown",
        "source_origin": "daily_bars",
        "source_row_id": bar.id,
        "source_updated_at": bar.created_at,
        "ingested_at": ingested_at,
        "batch_id": batch_id,
    }


def _universe_record(
    bar: UniverseDailyBar,
    symbol: UniverseSymbol,
    *,
    batch_id: str,
    adjust: str,
    ingested_at: datetime,
) -> dict[str, Any]:
    return {
        "symbol": _normalized_symbol(symbol.symbol),
        "trade_date": bar.trade_date,
        "adjust": adjust,
        "business_symbol_id": None,
        "universe_symbol_id": symbol.id,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": bar.amount,
        "turnover_rate": bar.turnover_rate,
        "source": bar.source or "unknown",
        "source_origin": "universe_daily_bars",
        "source_row_id": bar.id,
        "source_updated_at": bar.created_at,
        "ingested_at": ingested_at,
        "batch_id": batch_id,
    }


def _mirror_business_bars(
    db: Session,
    warehouse: FactorWarehouse,
    result: BarMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    adjust: str,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    if _cancelled(should_cancel):
        return
    source_key = _watermark_key(BUSINESS_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    # 一次性 COUNT 总行数（基于 cursor 过滤），用于进度计算
    total_rows = int(
        _read_scalar(db,
            select(func.count(DailyBar.id))
            .join(Symbol, Symbol.id == DailyBar.symbol_id)
            .where(
                DailyBar.id > cursor,
                Symbol.asset_type == "stock",
                Symbol.market.in_(("sh", "sz", "bj", "cn")),
                *_date_filters(DailyBar.trade_date, start_date, end_date),
            )
        )
        or 0
    )
    if progress_callback is not None:
        progress_callback(
            "business_bars", 0, total_rows,
            f"Mirroring business bars: 0/{total_rows}",
        )
    while True:
        if _cancelled(should_cancel):
            break
        stmt = (
            select(DailyBar, Symbol)
            .join(Symbol, Symbol.id == DailyBar.symbol_id)
            .where(
                DailyBar.id > cursor,
                Symbol.asset_type == "stock",
                Symbol.market.in_(("sh", "sz", "bj", "cn")),
                *_date_filters(DailyBar.trade_date, start_date, end_date),
            )
            .order_by(DailyBar.id)
            .limit(batch_size)
        )
        rows = _read_all(db, stmt)
        if not rows:
            break
        if _cancelled(should_cancel):
            break
        ingested_at = _utcnow_naive()
        records = [
            _business_record(
                bar,
                symbol,
                batch_id=result.batch_id,
                adjust=adjust,
                ingested_at=ingested_at,
            )
            for bar, symbol in rows
        ]
        cursor = int(rows[-1][0].id)
        result.business_rows += warehouse.upsert_daily_bars(
            records,
            source_key=source_key,
            watermark=cursor,
        )
        if progress_callback is not None:
            progress_callback(
                "business_bars",
                result.business_rows,
                total_rows,
                f"Mirroring business bars: {result.business_rows}/{total_rows}",
            )
    result.business_watermark = cursor


def _mirror_universe_bars(
    db: Session,
    warehouse: FactorWarehouse,
    result: BarMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    adjust: str,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    if _cancelled(should_cancel):
        return
    source_key = _watermark_key(UNIVERSE_SOURCE_KEY, start_date, end_date)
    if start_date is not None or end_date is not None:
        _mirror_universe_bars_by_symbol(
            db,
            warehouse,
            result,
            source_key=source_key,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            adjust=adjust,
            full_refresh=full_refresh,
            should_cancel=should_cancel,
            progress_callback=progress_callback,
        )
        return
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    # 一次性 COUNT 总行数
    total_rows = int(
        _read_scalar(db,
            select(func.count(UniverseDailyBar.id))
            .join(
                UniverseSymbol,
                UniverseSymbol.id == UniverseDailyBar.universe_symbol_id,
            )
            .where(
                UniverseDailyBar.id > cursor,
                UniverseSymbol.asset_type == "stock",
                UniverseSymbol.region == "cn",
                *_date_filters(
                    UniverseDailyBar.trade_date, start_date, end_date
                ),
            )
        )
        or 0
    )
    if progress_callback is not None:
        progress_callback(
            "universe_bars", 0, total_rows,
            f"Mirroring universe bars: 0/{total_rows}",
        )
    while True:
        if _cancelled(should_cancel):
            break
        stmt = (
            select(UniverseDailyBar, UniverseSymbol)
            .join(
                UniverseSymbol,
                UniverseSymbol.id == UniverseDailyBar.universe_symbol_id,
            )
            .where(
                UniverseDailyBar.id > cursor,
                UniverseSymbol.asset_type == "stock",
                UniverseSymbol.region == "cn",
                *_date_filters(
                    UniverseDailyBar.trade_date, start_date, end_date
                ),
            )
            .order_by(UniverseDailyBar.id)
            .limit(batch_size)
        )
        rows = _read_all(db, stmt)
        if not rows:
            break
        if _cancelled(should_cancel):
            break
        ingested_at = _utcnow_naive()
        records = [
            _universe_record(
                bar,
                symbol,
                batch_id=result.batch_id,
                adjust=adjust,
                ingested_at=ingested_at,
            )
            for bar, symbol in rows
        ]
        cursor = int(rows[-1][0].id)
        result.universe_rows += warehouse.upsert_daily_bars(
            records,
            source_key=source_key,
            watermark=cursor,
        )
        if progress_callback is not None:
            progress_callback(
                "universe_bars",
                result.universe_rows,
                total_rows,
                f"Mirroring universe bars: {result.universe_rows}/{total_rows}",
            )
    result.universe_watermark = cursor


def _mirror_universe_bars_by_symbol(
    db: Session,
    warehouse: FactorWarehouse,
    result: BarMirrorResult,
    *,
    source_key: str,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    adjust: str,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Use the existing (symbol_id, trade_date) index for bounded ranges."""
    symbol_ids = _read_scalars_all(db,
        select(UniverseSymbol.id)
        .where(
            UniverseSymbol.asset_type == "stock",
            UniverseSymbol.region == "cn",
        )
        .order_by(UniverseSymbol.id)
    )
    total_symbols = len(symbol_ids)
    if progress_callback is not None:
        progress_callback(
            "universe_bars", 0, total_symbols,
            f"Mirroring universe bars by symbol: 0/{total_symbols}",
        )
    symbol_batch_size = max(1, min(batch_size, 25))
    cursor = 0
    for offset in range(0, len(symbol_ids), symbol_batch_size):
        if _cancelled(should_cancel):
            break
        chunk = symbol_ids[offset : offset + symbol_batch_size]
        chunk_source_key = (
            f"{source_key}|symbols:{int(chunk[0])}-{int(chunk[-1])}"
        )
        chunk_cursor = (
            0
            if full_refresh
            else warehouse.get_watermark(chunk_source_key)
        )
        if progress_callback is not None:
            progress_callback(
                "universe_bars", offset, total_symbols,
                f"Reading universe bars for symbols {offset + 1}-{min(offset + symbol_batch_size, total_symbols)}",
            )
        rows = _read_all(db,
            select(UniverseDailyBar, UniverseSymbol)
            .join(
                UniverseSymbol,
                UniverseSymbol.id
                == UniverseDailyBar.universe_symbol_id,
            )
            .where(
                UniverseDailyBar.universe_symbol_id.in_(chunk),
                UniverseDailyBar.id > chunk_cursor,
                *_date_filters(
                    UniverseDailyBar.trade_date,
                    start_date,
                    end_date,
                ),
            )
            .order_by(
                UniverseDailyBar.universe_symbol_id,
                UniverseDailyBar.trade_date,
            )
        )
        processed = min(offset + symbol_batch_size, total_symbols)
        if not rows:
            # symbol 已处理但无数据，仍需推进进度
            if progress_callback is not None:
                progress_callback(
                    "universe_bars", processed, total_symbols,
                    f"Mirroring universe bars by symbol: {processed}/{total_symbols}",
                )
            continue
        if _cancelled(should_cancel):
            break
        if progress_callback is not None:
            progress_callback(
                "universe_bars", offset, total_symbols,
                f"Writing {len(rows)} universe bars to local warehouse",
            )
        ingested_at = _utcnow_naive()
        records = [
            _universe_record(
                bar,
                symbol,
                batch_id=result.batch_id,
                adjust=adjust,
                ingested_at=ingested_at,
            )
            for bar, symbol in rows
        ]
        chunk_cursor = max(int(bar.id) for bar, _symbol in rows)
        cursor = max(
            cursor, chunk_cursor
        )
        result.universe_rows += warehouse.upsert_daily_bars(
            records,
            source_key=chunk_source_key,
            watermark=chunk_cursor,
        )
        if progress_callback is not None:
            progress_callback(
                "universe_bars", processed, total_symbols,
                f"Mirroring universe bars by symbol: {processed}/{total_symbols}",
            )
    result.universe_watermark = cursor
    if not _cancelled(should_cancel):
        # Checkpoints only resume an interrupted bounded-range run. Clear them
        # after a complete pass so the next run can pick up source corrections
        # inside the same date range.
        warehouse.delete_watermarks(
            f"{source_key}|symbols:"
        )


def _mirror_universe_metadata(
    db: Session,
    warehouse: FactorWarehouse,
    result: BarMirrorResult,
    *,
    batch_size: int,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    if _cancelled(should_cancel):
        return
    cursor = (
        0
        if full_refresh
        else warehouse.get_watermark(UNIVERSE_METADATA_SOURCE_KEY)
    )
    # 一次性 COUNT 总行数
    total_rows = int(
        _read_scalar(db,
            select(func.count(UniverseSymbol.id)).where(
                UniverseSymbol.id > cursor
            )
        )
        or 0
    )
    if progress_callback is not None:
        progress_callback(
            "metadata", 0, total_rows,
            f"Mirroring universe metadata: 0/{total_rows}",
        )
    while True:
        if _cancelled(should_cancel):
            break
        rows = _read_scalars_all(db,
            select(UniverseSymbol)
            .where(UniverseSymbol.id > cursor)
            .order_by(UniverseSymbol.id)
            .limit(batch_size)
        )
        if not rows:
            break
        if _cancelled(should_cancel):
            break
        updated_at = _utcnow_naive()
        records = [
            {
                "symbol": _normalized_symbol(item.symbol),
                "name": item.name,
                "asset_type": item.asset_type,
                "market": item.market,
                "region": item.region,
                "is_active": True,
                "source": "sql:universe_symbols",
                "source_row_id": item.id,
                "updated_at": updated_at,
            }
            for item in rows
        ]
        cursor = int(rows[-1].id)
        result.metadata_rows += warehouse.upsert_records(
            "raw_asset_universe",
            records,
            source_key=UNIVERSE_METADATA_SOURCE_KEY,
            watermark=cursor,
        )
        if progress_callback is not None:
            progress_callback(
                "metadata",
                result.metadata_rows,
                total_rows,
                f"Mirroring universe metadata: {result.metadata_rows}/{total_rows}",
            )
    result.metadata_watermark = cursor


def mirror_daily_bars(
    db: Session,
    *,
    warehouse: FactorWarehouse | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    batch_size: int = 1000,
    adjust: str = "qfq",
    include_business: bool = True,
    include_universe: bool = True,
    full_refresh: bool = False,
    should_cancel: CancelCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> BarMirrorResult:
    """Mirror new SQL rows without making any network request.

    The two source tables use independent integer ID spaces. DuckDB therefore
    uses the normalized symbol code as its stable key while retaining both IDs
    for traceability.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not include_business and not include_universe:
        raise ValueError("at least one source must be enabled")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")

    result = BarMirrorResult(batch_id=f"bars-{uuid4().hex}")
    if _cancelled(should_cancel):
        return result
    target = warehouse or FactorWarehouse()
    target.initialize()
    if _cancelled(should_cancel):
        return result

    # WPD-06: best-effort batch audit. begin_batch errors are swallowed so
    # they never block the mirror pipeline. finalize_batch runs in finally
    # with the same guarantee.
    batch_recorded = False
    try:
        begin_batch(
            target,
            batch_id=result.batch_id,
            source_key="ingest.daily_bars.business",
            scope={
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "adjust": adjust,
                "include_business": include_business,
                "include_universe": include_universe,
                "full_refresh": full_refresh,
            },
        )
        batch_recorded = True
    except Exception:
        logger.exception(
            "begin_batch failed for %s; continuing without batch tracking",
            result.batch_id,
        )

    mirror_exc: Exception | None = None
    try:
        # Universe data is the full-market baseline. Business bars are mirrored
        # second so their business_symbol_id and any promoted-symbol corrections
        # remain visible on duplicate symbol/date keys.
        if include_universe:
            _mirror_universe_metadata(
                db,
                target,
                result,
                batch_size=batch_size,
                full_refresh=full_refresh,
                should_cancel=should_cancel,
                progress_callback=progress_callback,
            )
            if _cancelled(should_cancel):
                return result
            _mirror_universe_bars(
                db,
                target,
                result,
                start_date=start_date,
                end_date=end_date,
                batch_size=batch_size,
                adjust=adjust,
                full_refresh=full_refresh,
                should_cancel=should_cancel,
                progress_callback=progress_callback,
            )
        if include_business and not _cancelled(should_cancel):
            _mirror_business_bars(
                db,
                target,
                result,
                start_date=start_date,
                end_date=end_date,
                batch_size=batch_size,
                adjust=adjust,
                full_refresh=full_refresh,
                should_cancel=should_cancel,
                progress_callback=progress_callback,
            )
    except Exception as exc:
        mirror_exc = exc
        raise
    finally:
        if batch_recorded:
            try:
                if mirror_exc is not None:
                    finalize_batch(
                        target,
                        batch_id=result.batch_id,
                        status="failed",
                        rows_received=result.rows_written,
                        rows_written=result.rows_written,
                        error={
                            "error_type": type(mirror_exc).__name__,
                            "error_message": str(mirror_exc),
                        },
                    )
                else:
                    finalize_batch(
                        target,
                        batch_id=result.batch_id,
                        status="committed",
                        rows_received=result.rows_written,
                        rows_written=result.rows_written,
                    )
            except Exception:
                logger.exception(
                    "finalize_batch failed for %s", result.batch_id
                )
    return result
