"""Daily EastMoney stock-popularity snapshot synchronization."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
import akshare as ak
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.services.akshare_utils import (
    call_akshare_with_retry,
    quiet_akshare_output,
)
from app.services.factors.contracts import normalize_symbol
from app.services.factors.sentiment import normalize_hot_rank_frame
from app.services.market_data import _proxy_bypass


logger = logging.getLogger(__name__)
_API_KEY = "stock_hot_rank_em"


@dataclass(frozen=True)
class HotRankSyncResult:
    trade_date: date
    received: int
    written: int
    unmatched: int


def _symbol_code(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value))
    return match.group(1) if match else str(value).strip().upper()


def _raw_payloads(frame: pd.DataFrame) -> dict[str, str]:
    result: dict[str, str] = {}
    for record in frame.to_dict("records"):
        value = record.get("代码", record.get("股票代码", record.get("sc")))
        if value is None:
            continue
        result[normalize_symbol(value)] = json.dumps(
            record, ensure_ascii=False, default=str, sort_keys=True
        )
    return result


def _fetch_hot_rank(
    db: Session, as_of: date
) -> tuple[pd.DataFrame, dict[str, str]]:
    with _proxy_bypass(), quiet_akshare_output():
        frame = call_akshare_with_retry(
            ak.stock_hot_rank_em,
            api_key=_API_KEY,
            db=db,
        )
    if frame is None or frame.empty:
        return normalize_hot_rank_frame(pd.DataFrame(), as_of=as_of), {}
    return normalize_hot_rank_frame(
        frame, as_of=as_of
    ), _raw_payloads(frame)


def sync_hot_rank_snapshot(
    db: Session, *, as_of: date | None = None
) -> HotRankSyncResult:
    """Capture the provider's current top-100 list under one snapshot date."""
    trade_date = as_of or date.today()
    normalized, raw_payloads = _fetch_hot_rank(db, trade_date)
    written = 0
    unmatched = 0
    for row in normalized.to_dict("records"):
        code = _symbol_code(str(row["symbol"]))
        if re.fullmatch(r"\d{6}", code) is None:
            unmatched += 1
            continue
        source = str(row["source"])
        existing = db.execute(
            select(StockHotRankSnapshot).where(
                StockHotRankSnapshot.symbol == code,
                StockHotRankSnapshot.trade_date == trade_date,
                StockHotRankSnapshot.source == source,
            )
        ).scalars().first()
        if existing is None:
            existing = StockHotRankSnapshot(
                symbol=code,
                trade_date=trade_date,
                hot_rank=int(row["hot_rank"]),
                hot_rank_total=int(row["hot_rank_total"]),
                hot_rank_pct=float(row["hot_rank_pct"]),
                source=source,
            )
            db.add(existing)
        else:
            existing.hot_rank = int(row["hot_rank"])
            existing.hot_rank_total = int(row["hot_rank_total"])
            existing.hot_rank_pct = float(row["hot_rank_pct"])
        existing.raw_json = raw_payloads.get(code)
        written += 1
    db.flush()
    logger.info(
        "Hot-rank sync %s received=%d written=%d unmatched=%d",
        trade_date,
        len(normalized),
        written,
        unmatched,
    )
    return HotRankSyncResult(
        trade_date=trade_date,
        received=len(normalized),
        written=written,
        unmatched=unmatched,
    )


__all__ = ["HotRankSyncResult", "sync_hot_rank_snapshot"]
