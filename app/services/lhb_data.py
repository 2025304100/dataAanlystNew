"""Market-wide Dragon-Tiger institution-seat synchronization."""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import akshare as ak
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.services.akshare_utils import (
    call_akshare_with_retry,
    quiet_akshare_output,
)
from app.services.factors.capital_flow import (
    normalize_lhb_institution_frame,
)
from app.services.factors.contracts import normalize_symbol
from app.services.market_data import _proxy_bypass
from app.services.data_source_execution import source_task


logger = logging.getLogger(__name__)
_API_KEY = "stock_lhb_jgmmtj_em"
_MAX_RANGE_DAYS = 31


@dataclass(frozen=True)
class LhbInstitutionSyncResult:
    start_date: date
    end_date: date
    received: int
    written: int
    unmatched: int
    written_dates: tuple[date, ...] = ()
    skip_reason: str | None = None


def _symbol_code(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value))
    return match.group(1) if match else str(value).strip().upper()


def _raw_payloads(
    frame: pd.DataFrame,
) -> dict[tuple[str, date], str]:
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for record in frame.to_dict("records"):
        symbol_value = record.get("代码", record.get("SECURITY_CODE"))
        date_value = record.get("上榜日期", record.get("TRADE_DATE"))
        parsed = pd.to_datetime(date_value, errors="coerce")
        if symbol_value is None or pd.isna(parsed):
            continue
        key = (normalize_symbol(symbol_value), parsed.date())
        grouped.setdefault(key, []).append(record)
    return {
        key: json.dumps(
            records, ensure_ascii=False, default=str, sort_keys=True
        )
        for key, records in grouped.items()
    }


def _nullable_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _nullable_int(value: Any) -> int | None:
    numeric = _nullable_float(value)
    return int(numeric) if numeric is not None else None


def _fetch_lhb_institution(
    db: Session, start_date: date, end_date: date
) -> tuple[pd.DataFrame, dict[tuple[str, date], str]]:
    with _proxy_bypass(), quiet_akshare_output():
        frame = call_akshare_with_retry(
            ak.stock_lhb_jgmmtj_em,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            api_key=_API_KEY,
            db=db,
            max_attempts=2,
        )
    if frame is None or frame.empty:
        return normalize_lhb_institution_frame(pd.DataFrame()), {}
    return normalize_lhb_institution_frame(frame), _raw_payloads(frame)


@source_task(lane='incremental')
def sync_lhb_institution_trades(
    db: Session, *, start_date: date, end_date: date, fill_missing_only: bool = False
) -> LhbInstitutionSyncResult:
    """Synchronize one bounded market-wide date range."""
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if end_date > date.today():
        raise ValueError("龙虎榜结束日期不能晚于今天")
    if (end_date - start_date).days + 1 > _MAX_RANGE_DAYS:
        raise ValueError(
            f"date range must not exceed {_MAX_RANGE_DAYS} calendar days"
        )

    from app.services.market_session import range_is_cn_closed
    from app.models.market_data import MarketCalendar
    if range_is_cn_closed(db, start_date, end_date):
        return LhbInstitutionSyncResult(start_date, end_date, 0, 0, 0, skip_reason="market_closed")
    normalized, raw_payloads = _fetch_lhb_institution(db, start_date, end_date)
    # Some provider responses are empty or normalized by a compatibility
    # adapter to ``None``. Treat that as an empty result, not a subscript/type
    # error that aborts the whole async task.
    if normalized is None:
        normalized = normalize_lhb_institution_frame(pd.DataFrame())
    if raw_payloads is None:
        raw_payloads = {}
    written = 0
    unmatched = 0
    written_dates = set()
    closed_dates = set(db.execute(select(MarketCalendar.trade_date).where(
        MarketCalendar.trade_date >= start_date, MarketCalendar.trade_date <= end_date,
        MarketCalendar.is_trading_day == 0)).scalars())
    for row in normalized.to_dict("records"):
        code = _symbol_code(str(row["symbol"]))
        if re.fullmatch(r"\d{6}", code) is None:
            unmatched += 1
            continue
        trade_date = row["trade_date"]
        if not start_date <= trade_date <= end_date:
            raise ValueError("龙虎榜接口返回所选区间外的记录")
        if trade_date.weekday() >= 5 or trade_date in closed_dates or _nullable_float(row.get("institution_net")) is None:
            unmatched += 1
            continue
        source = str(row["source"])
        existing = db.execute(
            select(LhbInstitutionTrade).where(
                LhbInstitutionTrade.symbol == code,
                LhbInstitutionTrade.trade_date == trade_date,
                LhbInstitutionTrade.source == source,
            )
        ).scalars().first()
        was_existing = existing is not None
        if existing is None:
            existing = LhbInstitutionTrade(
                symbol=code,
                trade_date=trade_date,
                source=source,
            )
            db.add(existing)
        filled = []
        for field in ("buyer_institution_count", "seller_institution_count", "institution_buy", "institution_sell",
                      "institution_net", "market_amount", "institution_net_pct", "turnover_rate"):
            value = _nullable_int(row.get(field)) if field.endswith("_count") else _nullable_float(row.get(field))
            if value is not None and (not fill_missing_only or getattr(existing, field) is None):
                setattr(existing, field, value)
                filled.append(field)
        reason = row.get("reason")
        if reason is not None and not pd.isna(reason) and (not fill_missing_only or existing.reason is None):
            existing.reason = str(reason)
            filled.append("reason")
        if fill_missing_only and not filled:
            continue
        if fill_missing_only and was_existing:
            from app.services.sync_write_policy import record_missing_fill
            record_missing_fill(existing, filled, raw_payloads.get((code, trade_date)), source)
        else:
            existing.raw_json = raw_payloads.get((code, trade_date))
        written += 1
        written_dates.add(trade_date)
    db.flush()
    logger.info(
        "LHB institution sync %s..%s received=%d written=%d unmatched=%d",
        start_date,
        end_date,
        len(normalized),
        written,
        unmatched,
    )
    return LhbInstitutionSyncResult(
        start_date=start_date,
        end_date=end_date,
        received=len(normalized),
        written=written,
        unmatched=unmatched,
        written_dates=tuple(sorted(written_dates)),
    )


__all__ = [
    "LhbInstitutionSyncResult",
    "sync_lhb_institution_trades",
]
