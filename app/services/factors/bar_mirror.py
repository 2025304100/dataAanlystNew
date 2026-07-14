"""Incrementally mirror SQLAlchemy daily bars into the factor warehouse."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.factors.store import FactorWarehouse


BUSINESS_SOURCE_KEY = "sql.daily_bars"
UNIVERSE_SOURCE_KEY = "sql.universe_daily_bars"


@dataclass
class BarMirrorResult:
    batch_id: str
    business_rows: int = 0
    universe_rows: int = 0
    business_watermark: int = 0
    universe_watermark: int = 0

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


def _date_filters(column, start_date: date | None, end_date: date | None):
    filters = []
    if start_date is not None:
        filters.append(column >= start_date)
    if end_date is not None:
        filters.append(column <= end_date)
    return filters


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
) -> None:
    source_key = _watermark_key(BUSINESS_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        stmt = (
            select(DailyBar, Symbol)
            .join(Symbol, Symbol.id == DailyBar.symbol_id)
            .where(
                DailyBar.id > cursor,
                *_date_filters(DailyBar.trade_date, start_date, end_date),
            )
            .order_by(DailyBar.id)
            .limit(batch_size)
        )
        rows = db.execute(stmt).all()
        if not rows:
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
) -> None:
    source_key = _watermark_key(UNIVERSE_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        stmt = (
            select(UniverseDailyBar, UniverseSymbol)
            .join(
                UniverseSymbol,
                UniverseSymbol.id == UniverseDailyBar.universe_symbol_id,
            )
            .where(
                UniverseDailyBar.id > cursor,
                *_date_filters(
                    UniverseDailyBar.trade_date, start_date, end_date
                ),
            )
            .order_by(UniverseDailyBar.id)
            .limit(batch_size)
        )
        rows = db.execute(stmt).all()
        if not rows:
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
    result.universe_watermark = cursor


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

    target = warehouse or FactorWarehouse()
    target.initialize()
    result = BarMirrorResult(batch_id=f"bars-{uuid4().hex}")

    # Universe data is the full-market baseline. Business bars are mirrored
    # second so their business_symbol_id and any promoted-symbol corrections
    # remain visible on duplicate symbol/date keys.
    if include_universe:
        _mirror_universe_bars(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            adjust=adjust,
            full_refresh=full_refresh,
        )
    if include_business:
        _mirror_business_bars(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            adjust=adjust,
            full_refresh=full_refresh,
        )
    return result
