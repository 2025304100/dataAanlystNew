"""Market-wide Dragon-Tiger institution-seat synchronization."""
from __future__ import annotations

import json
import logging
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
    return float(value)


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
        )
    if frame is None or frame.empty:
        return normalize_lhb_institution_frame(pd.DataFrame()), {}
    return normalize_lhb_institution_frame(frame), _raw_payloads(frame)


def sync_lhb_institution_trades(
    db: Session, *, start_date: date, end_date: date
) -> LhbInstitutionSyncResult:
    """Synchronize one bounded market-wide date range."""
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if (end_date - start_date).days + 1 > _MAX_RANGE_DAYS:
        raise ValueError(
            f"date range must not exceed {_MAX_RANGE_DAYS} calendar days"
        )

    normalized, raw_payloads = _fetch_lhb_institution(
        db, start_date, end_date
    )
    written = 0
    unmatched = 0
    for row in normalized.to_dict("records"):
        code = _symbol_code(str(row["symbol"]))
        if re.fullmatch(r"\d{6}", code) is None:
            unmatched += 1
            continue
        trade_date = row["trade_date"]
        source = str(row["source"])
        existing = db.execute(
            select(LhbInstitutionTrade).where(
                LhbInstitutionTrade.symbol == code,
                LhbInstitutionTrade.trade_date == trade_date,
                LhbInstitutionTrade.source == source,
            )
        ).scalars().first()
        if existing is None:
            existing = LhbInstitutionTrade(
                symbol=code,
                trade_date=trade_date,
                source=source,
            )
            db.add(existing)
        existing.buyer_institution_count = _nullable_int(
            row["buyer_institution_count"]
        )
        existing.seller_institution_count = _nullable_int(
            row["seller_institution_count"]
        )
        existing.institution_buy = _nullable_float(
            row["institution_buy"]
        )
        existing.institution_sell = _nullable_float(
            row["institution_sell"]
        )
        existing.institution_net = _nullable_float(
            row["institution_net"]
        )
        existing.market_amount = _nullable_float(row["market_amount"])
        existing.institution_net_pct = _nullable_float(
            row["institution_net_pct"]
        )
        existing.turnover_rate = _nullable_float(row["turnover_rate"])
        existing.reason = (
            str(row["reason"]) if not pd.isna(row["reason"]) else None
        )
        existing.raw_json = raw_payloads.get((code, trade_date))
        written += 1
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
    )


__all__ = [
    "LhbInstitutionSyncResult",
    "sync_lhb_institution_trades",
]
