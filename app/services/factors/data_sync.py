"""Mirror existing business data into the local factor warehouse.

This module deliberately has no AkShare dependency. Network-backed services
populate the SQL business tables first; research and factor calculation then
operate only on the local DuckDB mirror.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.capital_flow import CapitalFlow
from app.models.macro_data import MacroIndicatorValue
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services.factors.store import FactorWarehouse


VALUATION_SOURCE_KEY = "sql.stock_valuations"
FUND_FLOW_SOURCE_KEY = "sql.capital_flows"
MACRO_SOURCE_KEY = "sql.macro_indicator_values"


@dataclass
class FactorInputMirrorResult:
    batch_id: str
    valuation_rows: int = 0
    fund_flow_rows: int = 0
    macro_rows: int = 0
    valuation_watermark: int = 0
    fund_flow_watermark: int = 0
    macro_watermark: int = 0

    @property
    def rows_written(self) -> int:
        return self.valuation_rows + self.fund_flow_rows + self.macro_rows


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _watermark_key(
    source_key: str, start_date: date | None, end_date: date | None
) -> str:
    if start_date is None and end_date is None:
        return source_key
    return (
        f"{source_key}:"
        f"{start_date.isoformat() if start_date else '*'}:"
        f"{end_date.isoformat() if end_date else '*'}"
    )


def _date_filters(column: Any, start_date: date | None, end_date: date | None):
    filters = []
    if start_date is not None:
        filters.append(column >= start_date)
    if end_date is not None:
        filters.append(column <= end_date)
    return filters


def _symbol_code(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value))
    return match.group(1) if match else str(value).strip().upper()


def _source_hash(raw_json: str | None, fallback: dict[str, Any]) -> str:
    payload = raw_json or json.dumps(
        fallback, ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _period_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    match = re.search(
        r"(?P<year>\d{4})\D+(?P<month>\d{1,2})(?:\D+(?P<day>\d{1,2}))?",
        text,
    )
    if match is None:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day") or 1),
        )
    except ValueError:
        return None


def _mirror_valuations(
    db: Session,
    warehouse: FactorWarehouse,
    result: FactorInputMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    full_refresh: bool,
) -> None:
    source_key = _watermark_key(VALUATION_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        rows = db.execute(
            select(StockValuation, Symbol)
            .join(Symbol, Symbol.id == StockValuation.symbol_id)
            .where(
                StockValuation.id > cursor,
                *_date_filters(
                    StockValuation.trade_date, start_date, end_date
                ),
            )
            .order_by(StockValuation.id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        ingested_at = _utcnow_naive()
        records = []
        for valuation, symbol in rows:
            fallback = {
                "pe_ttm": valuation.pe_ttm,
                "pb": valuation.pb,
                "total_market_cap": valuation.total_market_cap,
                "circulating_market_cap": valuation.circulating_market_cap,
            }
            records.append(
                {
                    "symbol": _symbol_code(symbol.symbol),
                    "trade_date": valuation.trade_date,
                    "pe_ttm": valuation.pe_ttm,
                    "pb": valuation.pb,
                    "dividend_yield": None,
                    "total_market_cap": valuation.total_market_cap,
                    "circulating_market_cap": valuation.circulating_market_cap,
                    "source": valuation.source or "business_sql",
                    "source_hash": _source_hash(
                        valuation.raw_json, fallback
                    ),
                    "ingested_at": ingested_at,
                    "batch_id": result.batch_id,
                }
            )
        cursor = int(rows[-1][0].id)
        result.valuation_rows += warehouse.upsert_records(
            "raw_valuation_snapshots",
            records,
            source_key=source_key,
            watermark=cursor,
        )
    result.valuation_watermark = cursor


def _mirror_fund_flows(
    db: Session,
    warehouse: FactorWarehouse,
    result: FactorInputMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    full_refresh: bool,
) -> None:
    source_key = _watermark_key(FUND_FLOW_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        rows = db.execute(
            select(CapitalFlow, Symbol)
            .join(Symbol, Symbol.id == CapitalFlow.symbol_id)
            .where(
                CapitalFlow.id > cursor,
                *_date_filters(CapitalFlow.trade_date, start_date, end_date),
            )
            .order_by(CapitalFlow.id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        ingested_at = _utcnow_naive()
        records = [
            {
                "symbol": _symbol_code(symbol.symbol),
                "trade_date": flow.trade_date,
                "main_net_inflow": flow.main_net_inflow,
                "main_net_inflow_pct": flow.main_net_inflow_pct,
                "super_large_net_inflow": flow.super_large_net_inflow,
                "large_net_inflow": flow.large_net_inflow,
                "medium_net_inflow": flow.medium_net_inflow,
                "small_net_inflow": flow.small_net_inflow,
                "source": flow.source or "business_sql",
                "ingested_at": ingested_at,
                "batch_id": result.batch_id,
            }
            for flow, symbol in rows
        ]
        cursor = int(rows[-1][0].id)
        result.fund_flow_rows += warehouse.upsert_records(
            "raw_fund_flows",
            records,
            source_key=source_key,
            watermark=cursor,
        )
    result.fund_flow_watermark = cursor


def _mirror_macro(
    db: Session,
    warehouse: FactorWarehouse,
    result: FactorInputMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    full_refresh: bool,
) -> None:
    source_key = _watermark_key(MACRO_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        rows = db.execute(
            select(MacroIndicatorValue)
            .where(MacroIndicatorValue.id > cursor)
            .order_by(MacroIndicatorValue.id)
            .limit(batch_size)
        ).scalars().all()
        if not rows:
            break
        ingested_at = _utcnow_naive()
        records = []
        for indicator in rows:
            period = _period_date(indicator.period)
            if period is None:
                continue
            if start_date is not None and period < start_date:
                continue
            if end_date is not None and period > end_date:
                continue
            records.append(
                {
                    "indicator_key": indicator.indicator_key,
                    "period": period,
                    "value": indicator.value,
                    "previous_value": indicator.previous_value,
                    "source": indicator.source or "business_sql",
                    "ingested_at": ingested_at,
                    "batch_id": result.batch_id,
                }
            )
        cursor = int(rows[-1].id)
        result.macro_rows += warehouse.upsert_records(
            "raw_macro",
            records,
            source_key=source_key,
            watermark=cursor,
        )
    result.macro_watermark = cursor


def mirror_factor_inputs(
    db: Session,
    *,
    warehouse: FactorWarehouse | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    batch_size: int = 1000,
    include_valuations: bool = True,
    include_fund_flows: bool = True,
    include_macro: bool = True,
    full_refresh: bool = False,
) -> FactorInputMirrorResult:
    """Mirror local SQL factor inputs without making a network request."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not any((include_valuations, include_fund_flows, include_macro)):
        raise ValueError("at least one source must be enabled")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")

    target = warehouse or FactorWarehouse()
    target.initialize()
    result = FactorInputMirrorResult(batch_id=f"factor-inputs-{uuid4().hex}")
    if include_valuations:
        _mirror_valuations(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
        )
    if include_fund_flows:
        _mirror_fund_flows(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
        )
    if include_macro:
        _mirror_macro(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
        )
    return result
