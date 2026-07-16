"""A-share financial report synchronization for point-in-time factors."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any

import akshare as ak
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.financial_report import StockFinancialReport
from app.models.symbol import Symbol
from app.services.akshare_utils import (
    call_akshare_with_retry,
    quiet_akshare_output,
)
from app.services.factors.fundamental import (
    normalize_financial_analysis_frame,
)
from app.services.market_data import _proxy_bypass
from app.services.regions import region_from_market


logger = logging.getLogger(__name__)
_FINANCIAL_API_KEY = "stock_financial_analysis_indicator_em"
_REPORT_PERIOD_ALIASES = ("REPORT_DATE", "报告期", "报告日期")
_ANNOUNCEMENT_DATE_ALIASES = (
    "NOTICE_DATE",
    "公告日期",
    "最新公告日期",
)


def _symbol_code(symbol: Symbol) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(symbol.symbol))
    return match.group(1) if match else str(symbol.symbol).strip()


def _akshare_financial_symbol(symbol: Symbol) -> str:
    """Return the exchange-suffixed code required by EastMoney F10."""
    text = str(symbol.symbol).strip().upper()
    code = _symbol_code(symbol)
    suffix_match = re.search(r"\.(SH|SZ|BJ)$", text)
    if suffix_match:
        return f"{code}.{suffix_match.group(1)}"
    market = str(symbol.market or "").strip().upper()
    if market in {"SH", "SZ", "BJ"}:
        return f"{code}.{market}"
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    return f"{code}.BJ"


def _date_from_raw(record: dict[str, Any], aliases: tuple[str, ...]) -> date | None:
    value = next((record[key] for key in aliases if key in record), None)
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if isinstance(parsed, datetime):
        return parsed.date()
    return parsed.date()


def _raw_rows_by_identity(frame: pd.DataFrame) -> dict[tuple[date, date], str]:
    """Preserve the provider payload for audit and schema-drift diagnosis."""
    result: dict[tuple[date, date], str] = {}
    for record in frame.to_dict("records"):
        report_period = _date_from_raw(record, _REPORT_PERIOD_ALIASES)
        announcement_date = _date_from_raw(
            record, _ANNOUNCEMENT_DATE_ALIASES
        )
        if report_period is None or announcement_date is None:
            continue
        result[(report_period, announcement_date)] = json.dumps(
            record, ensure_ascii=False, default=str, sort_keys=True
        )
    return result


def _nullable_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _fetch_financial_analysis(
    db: Session, symbol: Symbol
) -> tuple[pd.DataFrame, dict[tuple[date, date], str]]:
    code = _akshare_financial_symbol(symbol)
    with _proxy_bypass(), quiet_akshare_output():
        frame = call_akshare_with_retry(
            ak.stock_financial_analysis_indicator_em,
            symbol=code,
            indicator="按报告期",
            api_key=_FINANCIAL_API_KEY,
            db=db,
        )
    if frame is None or frame.empty:
        return normalize_financial_analysis_frame(
            pd.DataFrame(), symbol=code
        ), {}
    return (
        normalize_financial_analysis_frame(frame, symbol=code),
        _raw_rows_by_identity(frame),
    )


def sync_symbol_financial_reports(db: Session, symbol: Symbol) -> int:
    """Fetch and upsert all available report versions for one A-share."""
    if symbol.asset_type != "stock":
        return 0
    if region_from_market(symbol.market) != "cn":
        return 0

    normalized, raw_rows = _fetch_financial_analysis(db, symbol)
    written = 0
    for row in normalized.to_dict("records"):
        report_period = row["report_period"]
        announcement_date = row["announcement_date"]
        report_type = str(row["report_type"])
        source = str(row["source"])
        existing = db.execute(
            select(StockFinancialReport).where(
                StockFinancialReport.symbol_id == symbol.id,
                StockFinancialReport.report_period == report_period,
                StockFinancialReport.announcement_date
                == announcement_date,
                StockFinancialReport.report_type == report_type,
                StockFinancialReport.source == source,
            )
        ).scalars().first()
        if existing is None:
            existing = StockFinancialReport(
                symbol_id=symbol.id,
                report_period=report_period,
                announcement_date=announcement_date,
                report_type=report_type,
                source=source,
            )
            db.add(existing)
        existing.roe_ttm = _nullable_float(row["roe_ttm"])
        existing.net_profit = _nullable_float(row["net_profit"])
        existing.revenue = _nullable_float(row["revenue"])
        existing.net_profit_yoy = _nullable_float(row["net_profit_yoy"])
        existing.revenue_yoy = _nullable_float(row["revenue_yoy"])
        existing.raw_json = raw_rows.get(
            (report_period, announcement_date)
        )
        written += 1
    db.flush()
    logger.info(
        "Synced %d financial report versions for %s",
        written,
        symbol.symbol,
    )
    return written


def resolve_financial_report_symbols(
    db: Session, *, source: str, limit: int
) -> list[Symbol]:
    """Resolve a bounded A-share scope for unattended report syncing."""
    if source not in {"watchlist", "positions", "all"}:
        raise ValueError(f"unsupported financial report source: {source}")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    stmt = select(Symbol).where(
        Symbol.asset_type == "stock",
        Symbol.is_active == 1,
        Symbol.market.in_(("sh", "sz", "bj", "cn")),
    )
    if source == "watchlist":
        from app.models.watchlist import WatchlistItem

        stmt = stmt.join(
            WatchlistItem,
            WatchlistItem.symbol_id == Symbol.id,
        )
    elif source == "positions":
        from app.models.portfolio import Position

        stmt = stmt.join(Position, Position.symbol_id == Symbol.id)
    rows = db.execute(
        stmt.distinct().order_by(Symbol.id).limit(limit)
    ).scalars().all()
    return [
        symbol
        for symbol in rows
        if region_from_market(symbol.market) == "cn"
    ]


__all__ = [
    "_akshare_financial_symbol",
    "resolve_financial_report_symbols",
    "sync_symbol_financial_reports",
]
