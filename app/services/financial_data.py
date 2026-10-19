"""A-share financial report synchronization for point-in-time factors."""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.financial_report import StockFinancialReport
from app.models.symbol import Symbol
from app.services.factors.fundamental import (
    normalize_financial_analysis_frame,
)
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
    number = float(value)
    return number if math.isfinite(number) else None


def _fetch_financial_analysis(
    db: Session, symbol: Symbol, *, start: date | None = None, end: date | None = None,
) -> tuple[pd.DataFrame, dict[tuple[date, date], str]]:
    from app.services.financial_provider import fetch_financial_rows
    code = _akshare_financial_symbol(symbol)
    frame = pd.DataFrame(fetch_financial_rows(db, code, start or date(1990, 1, 1), end or date.today()))
    if frame is None or frame.empty:
        return normalize_financial_analysis_frame(
            pd.DataFrame(), symbol=code
        ), {}
    return (
        normalize_financial_analysis_frame(frame, symbol=code),
        _raw_rows_by_identity(frame),
    )


def sync_symbol_financial_reports(
    db: Session,
    symbol: Symbol,
    *,
    announcement_start: date | None = None,
    announcement_end: date | None = None,
    incremental: bool = False,
) -> int:
    """Fetch and upsert report versions within an optional announcement range."""
    if symbol.asset_type != "stock":
        return 0
    if region_from_market(symbol.market) != "cn":
        return 0
    if (
        announcement_start is not None
        and announcement_end is not None
        and announcement_start > announcement_end
    ):
        raise ValueError("announcement_start must be on or before announcement_end")

    effective_end = announcement_end or date.today()
    if effective_end > date.today():
        raise ValueError("财报公告日期不能晚于今天")
    if incremental and announcement_start is None:
        announcement_start = effective_end
    normalized, raw_rows = _fetch_financial_analysis(db, symbol, start=announcement_start, end=effective_end)
    return _persist_financial_analysis(db, symbol, normalized, raw_rows, announcement_start, effective_end)


def _persist_financial_analysis(db, symbol, normalized, raw_rows, announcement_start, effective_end):
    written = 0
    written_dates = []
    for row in normalized.to_dict("records"):
        report_period = row["report_period"]
        announcement_date = row["announcement_date"]
        if announcement_start is not None and announcement_date < announcement_start:
            continue
        if announcement_date > effective_end or report_period > announcement_date:
            continue
        values = {field: _nullable_float(row[field]) for field in
                  ("roe_ttm", "net_profit", "revenue", "net_profit_yoy", "revenue_yoy")}
        if not any(value is not None for value in values.values()):
            continue
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
        changed = existing.id is None
        for field, value in values.items():
            if value is not None and getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
        if not changed:
            continue
        existing.raw_json = raw_rows.get((report_period, announcement_date))
        written += 1
        written_dates.append(announcement_date)
    db.flush()
    logger.info(
        "Synced %d financial report versions for %s",
        written,
        symbol.symbol,
    )
    return FinancialWriteCount(written, written_dates)


class FinancialWriteCount(int):
    def __new__(cls, count, dates):
        value = super().__new__(cls, count)
        value.sync_written_dates = tuple(dates)
        return value


def sync_financial_announcements(db, symbols, start, end, *, should_cancel=None, on_result=None):
    """Daily scope is fetched in code/date batches; unchanged reports are skipped."""
    from app.services.financial_provider import BATCH_SIZE, fetch_financial_rows
    eligible = [s for s in symbols if s.asset_type == "stock" and region_from_market(s.market) == "cn"]
    result = {"total": len(eligible), "success": 0, "skipped": 0, "failed": 0, "records": 0, "errors": []}
    observations = []
    for offset in range(0, len(eligible), BATCH_SIZE):
        batch = eligible[offset:offset+BATCH_SIZE]
        observations.extend(fetch_financial_rows(db, [_akshare_financial_symbol(s) for s in batch], start, end, should_cancel=should_cancel))
    for symbol in eligible:
        if should_cancel and should_cancel():
            raise InterruptedError("财报同步已取消")
        code = _akshare_financial_symbol(symbol)
        frame = pd.DataFrame([row for row in observations if row["SECUCODE"] == code])
        normalized = normalize_financial_analysis_frame(frame, symbol=code)
        count = _persist_financial_analysis(db, symbol, normalized, _raw_rows_by_identity(frame), start, end)
        result["success" if count else "skipped"] += 1
        result["records"] += int(count)
        dates = count.sync_written_dates
        if dates:
            result["written_start_date"] = min(result.get("written_start_date", min(dates).isoformat()), min(dates).isoformat())
            result["written_end_date"] = max(result.get("written_end_date", max(dates).isoformat()), max(dates).isoformat())
        if on_result:
            on_result(symbol, count, result)
    return result


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
