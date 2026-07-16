"""Mirror existing business data into the local factor warehouse.

This module deliberately has no AkShare dependency. Network-backed services
populate the SQL business tables first; research and factor calculation then
operate only on the local DuckDB mirror.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.capital_flow import CapitalFlow
from app.models.financial_report import StockFinancialReport
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.macro_data import MacroIndicatorValue
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import (
    TailAccumulationSnapshot,
)
from app.services.factors.store import FactorWarehouse


VALUATION_SOURCE_KEY = "sql.stock_valuations"
FUND_FLOW_SOURCE_KEY = "sql.capital_flows"
FINANCIAL_REPORT_SOURCE_KEY = "sql.stock_financial_reports"
LHB_INSTITUTION_SOURCE_KEY = "sql.lhb_institution_trades"
HOT_RANK_SOURCE_KEY = "sql.stock_hot_rank_snapshots"
TAIL_PROXY_SOURCE_KEY = "sql.tail_accumulation_snapshots"
MACRO_SOURCE_KEY = "sql.macro_indicator_values"
CancelCheck = Callable[[], bool]


@dataclass
class FactorInputMirrorResult:
    batch_id: str
    valuation_rows: int = 0
    financial_report_rows: int = 0
    lhb_institution_rows: int = 0
    hot_rank_rows: int = 0
    tail_proxy_rows: int = 0
    fund_flow_rows: int = 0
    macro_rows: int = 0
    valuation_watermark: int = 0
    financial_report_watermark: int = 0
    lhb_institution_watermark: int = 0
    hot_rank_watermark: int = 0
    tail_proxy_watermark: int = 0
    fund_flow_watermark: int = 0
    macro_watermark: int = 0

    @property
    def sentiment_rows(self) -> int:
        return self.lhb_institution_rows + self.hot_rank_rows

    @property
    def rows_written(self) -> int:
        return (
            self.valuation_rows
            + self.financial_report_rows
            + self.sentiment_rows
            + self.tail_proxy_rows
            + self.fund_flow_rows
            + self.macro_rows
        )


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cancelled(should_cancel: CancelCheck | None) -> bool:
    return bool(should_cancel and should_cancel())


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
    should_cancel: CancelCheck | None,
) -> None:
    if _cancelled(should_cancel):
        return
    source_key = _watermark_key(VALUATION_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        if _cancelled(should_cancel):
            break
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
        if _cancelled(should_cancel):
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
    should_cancel: CancelCheck | None,
) -> None:
    if _cancelled(should_cancel):
        return
    source_key = _watermark_key(FUND_FLOW_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        if _cancelled(should_cancel):
            break
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
        if _cancelled(should_cancel):
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


def _mirror_financial_reports(
    db: Session,
    warehouse: FactorWarehouse,
    result: FactorInputMirrorResult,
    *,
    end_date: date | None,
    batch_size: int,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
) -> None:
    if _cancelled(should_cancel):
        return
    # Reports announced before start_date remain necessary to establish the
    # latest visible quarter and its same-period prior-year value.
    source_key = _watermark_key(
        FINANCIAL_REPORT_SOURCE_KEY, None, end_date
    )
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        if _cancelled(should_cancel):
            break
        filters = [StockFinancialReport.id > cursor]
        if end_date is not None:
            filters.append(
                StockFinancialReport.announcement_date <= end_date
            )
        rows = db.execute(
            select(StockFinancialReport, Symbol)
            .join(Symbol, Symbol.id == StockFinancialReport.symbol_id)
            .where(*filters)
            .order_by(StockFinancialReport.id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        if _cancelled(should_cancel):
            break
        ingested_at = _utcnow_naive()
        records = []
        for report, symbol in rows:
            fallback = {
                "report_period": report.report_period,
                "announcement_date": report.announcement_date,
                "report_type": report.report_type,
                "roe_ttm": report.roe_ttm,
                "net_profit": report.net_profit,
                "revenue": report.revenue,
                "net_profit_yoy": report.net_profit_yoy,
                "revenue_yoy": report.revenue_yoy,
            }
            records.append(
                {
                    "symbol": _symbol_code(symbol.symbol),
                    "report_period": report.report_period,
                    "announcement_date": report.announcement_date,
                    "report_type": report.report_type,
                    "roe_ttm": report.roe_ttm,
                    "net_profit": report.net_profit,
                    "revenue": report.revenue,
                    "net_profit_yoy": report.net_profit_yoy,
                    "revenue_yoy": report.revenue_yoy,
                    "source": report.source or "business_sql",
                    "source_hash": _source_hash(
                        report.raw_json, fallback
                    ),
                    "ingested_at": ingested_at,
                    "batch_id": result.batch_id,
                }
            )
        cursor = int(rows[-1][0].id)
        result.financial_report_rows += warehouse.upsert_records(
            "raw_financial_reports",
            records,
            source_key=source_key,
            watermark=cursor,
        )
    result.financial_report_watermark = cursor


def _mirror_macro(
    db: Session,
    warehouse: FactorWarehouse,
    result: FactorInputMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
) -> None:
    if _cancelled(should_cancel):
        return
    source_key = _watermark_key(MACRO_SOURCE_KEY, start_date, end_date)
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        if _cancelled(should_cancel):
            break
        rows = db.execute(
            select(MacroIndicatorValue)
            .where(MacroIndicatorValue.id > cursor)
            .order_by(MacroIndicatorValue.id)
            .limit(batch_size)
        ).scalars().all()
        if not rows:
            break
        if _cancelled(should_cancel):
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


def _mirror_lhb_institution(
    db: Session,
    warehouse: FactorWarehouse,
    result: FactorInputMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
) -> None:
    if _cancelled(should_cancel):
        return
    source_key = _watermark_key(
        LHB_INSTITUTION_SOURCE_KEY, start_date, end_date
    )
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        if _cancelled(should_cancel):
            break
        rows = db.execute(
            select(LhbInstitutionTrade)
            .where(
                LhbInstitutionTrade.id > cursor,
                *_date_filters(
                    LhbInstitutionTrade.trade_date,
                    start_date,
                    end_date,
                ),
            )
            .order_by(LhbInstitutionTrade.id)
            .limit(batch_size)
        ).scalars().all()
        if not rows:
            break
        if _cancelled(should_cancel):
            break
        ingested_at = _utcnow_naive()
        records = [
            {
                "symbol": _symbol_code(item.symbol),
                "trade_date": item.trade_date,
                "hot_rank": None,
                "hot_rank_total": None,
                "hot_rank_pct": None,
                "has_lhb": True,
                "lhb_institution_net": item.institution_net,
                "source": item.source or "business_sql",
                "ingested_at": ingested_at,
                "batch_id": result.batch_id,
            }
            for item in rows
        ]
        cursor = int(rows[-1].id)
        result.lhb_institution_rows += warehouse.upsert_records(
            "raw_sentiment",
            records,
            source_key=source_key,
            watermark=cursor,
        )
    result.lhb_institution_watermark = cursor


def _mirror_hot_rank(
    db: Session,
    warehouse: FactorWarehouse,
    result: FactorInputMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
) -> None:
    if _cancelled(should_cancel):
        return
    source_key = _watermark_key(
        HOT_RANK_SOURCE_KEY, start_date, end_date
    )
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        if _cancelled(should_cancel):
            break
        rows = db.execute(
            select(StockHotRankSnapshot)
            .where(
                StockHotRankSnapshot.id > cursor,
                *_date_filters(
                    StockHotRankSnapshot.trade_date,
                    start_date,
                    end_date,
                ),
            )
            .order_by(StockHotRankSnapshot.id)
            .limit(batch_size)
        ).scalars().all()
        if not rows:
            break
        if _cancelled(should_cancel):
            break
        ingested_at = _utcnow_naive()
        records = [
            {
                "symbol": _symbol_code(item.symbol),
                "trade_date": item.trade_date,
                "hot_rank": item.hot_rank,
                "hot_rank_total": item.hot_rank_total,
                "hot_rank_pct": item.hot_rank_pct,
                "has_lhb": False,
                "lhb_institution_net": None,
                "source": item.source or "business_sql",
                "ingested_at": ingested_at,
                "batch_id": result.batch_id,
            }
            for item in rows
        ]
        cursor = int(rows[-1].id)
        result.hot_rank_rows += warehouse.upsert_records(
            "raw_sentiment",
            records,
            source_key=source_key,
            watermark=cursor,
        )
    result.hot_rank_watermark = cursor


def _mirror_tail_proxy(
    db: Session,
    warehouse: FactorWarehouse,
    result: FactorInputMirrorResult,
    *,
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    full_refresh: bool,
    should_cancel: CancelCheck | None,
) -> None:
    if _cancelled(should_cancel):
        return
    source_key = _watermark_key(
        TAIL_PROXY_SOURCE_KEY, start_date, end_date
    )
    cursor = 0 if full_refresh else warehouse.get_watermark(source_key)
    while True:
        if _cancelled(should_cancel):
            break
        rows = db.execute(
            select(TailAccumulationSnapshot)
            .where(
                TailAccumulationSnapshot.id > cursor,
                *_date_filters(
                    TailAccumulationSnapshot.trade_date,
                    start_date,
                    end_date,
                ),
            )
            .order_by(TailAccumulationSnapshot.id)
            .limit(batch_size)
        ).scalars().all()
        if not rows:
            break
        if _cancelled(should_cancel):
            break
        ingested_at = _utcnow_naive()
        records = [
            {
                "symbol": _symbol_code(item.symbol),
                "trade_date": item.trade_date,
                "minute_count": item.minute_count,
                "tail_minute_count": item.tail_minute_count,
                "day_amount": item.day_amount,
                "tail_amount": item.tail_amount,
                "tail_amount_share": item.tail_amount_share,
                "tail_activity_ratio": item.tail_activity_ratio,
                "tail_return": item.tail_return,
                "close_location": item.close_location,
                "proxy_score": item.proxy_score,
                "source": item.source or "business_sql",
                "ingested_at": ingested_at,
                "batch_id": result.batch_id,
            }
            for item in rows
        ]
        cursor = int(rows[-1].id)
        result.tail_proxy_rows += warehouse.upsert_records(
            "raw_tail_proxy",
            records,
            source_key=source_key,
            watermark=cursor,
        )
    result.tail_proxy_watermark = cursor


def mirror_factor_inputs(
    db: Session,
    *,
    warehouse: FactorWarehouse | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    batch_size: int = 1000,
    include_valuations: bool = True,
    include_financial_reports: bool = True,
    include_fund_flows: bool = True,
    include_sentiment: bool = True,
    include_tail_proxy: bool = True,
    include_macro: bool = True,
    full_refresh: bool = False,
    should_cancel: CancelCheck | None = None,
) -> FactorInputMirrorResult:
    """Mirror local SQL factor inputs without making a network request."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not any(
        (
            include_valuations,
            include_financial_reports,
            include_fund_flows,
            include_sentiment,
            include_tail_proxy,
            include_macro,
        )
    ):
        raise ValueError("at least one source must be enabled")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")

    result = FactorInputMirrorResult(batch_id=f"factor-inputs-{uuid4().hex}")
    if _cancelled(should_cancel):
        return result
    target = warehouse or FactorWarehouse()
    target.initialize()
    if _cancelled(should_cancel):
        return result
    if include_valuations:
        _mirror_valuations(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
            should_cancel=should_cancel,
        )
    if include_financial_reports and not _cancelled(should_cancel):
        _mirror_financial_reports(
            db,
            target,
            result,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
            should_cancel=should_cancel,
        )
    if include_fund_flows and not _cancelled(should_cancel):
        _mirror_fund_flows(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
            should_cancel=should_cancel,
        )
    if include_sentiment and not _cancelled(should_cancel):
        _mirror_lhb_institution(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
            should_cancel=should_cancel,
        )
        if not _cancelled(should_cancel):
            _mirror_hot_rank(
                db,
                target,
                result,
                start_date=start_date,
                end_date=end_date,
                batch_size=batch_size,
                full_refresh=full_refresh,
                should_cancel=should_cancel,
            )
    if include_tail_proxy and not _cancelled(should_cancel):
        _mirror_tail_proxy(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
            should_cancel=should_cancel,
        )
    if include_macro and not _cancelled(should_cancel):
        _mirror_macro(
            db,
            target,
            result,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            full_refresh=full_refresh,
            should_cancel=should_cancel,
        )
    return result
