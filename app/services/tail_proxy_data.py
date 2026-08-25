"""Candidate-scoped minute data synchronization for a tail-session proxy."""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import akshare as ak
import pandas as pd
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.discovery_candidate import DiscoveryCandidate
from app.models.daily_bar import DailyBar
from app.models.portfolio import Position
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import (
    TailAccumulationSnapshot,
)
from app.models.watchlist import WatchlistItem
from app.services.akshare_utils import (
    call_akshare_with_retry,
    quiet_akshare_output,
)
from app.services.factors.tail_proxy import calculate_tail_proxy
from app.services.market_data import _proxy_bypass
from app.services.regions import region_from_market


logger = logging.getLogger(__name__)
_API_KEY = "stock_zh_a_hist_min_em"
_SOURCE = "akshare:stock_zh_a_hist_min_em:1m"


@dataclass(frozen=True)
class TailProxySyncResult:
    trade_date: date
    source_scope: str
    total: int
    written: int
    skipped: int
    failed: int
    errors: tuple[str, ...] = ()


def _symbol_code(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value))
    return match.group(1) if match else str(value).strip().upper()


def resolve_tail_proxy_symbols(
    db: Session, *, source: str, limit: int
) -> list[str]:
    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    if source == "candidates":
        latest_run_id = db.scalar(
            select(func.max(DiscoveryCandidate.scan_run_id))
        )
        if latest_run_id is None:
            return []
        rows = db.execute(
            select(DiscoveryCandidate.symbol)
            .where(
                DiscoveryCandidate.scan_run_id == latest_run_id,
                DiscoveryCandidate.asset_type == "stock",
            )
            .order_by(
                desc(DiscoveryCandidate.priority_score),
                DiscoveryCandidate.id,
            )
            .limit(limit)
        ).scalars().all()
        return list(dict.fromkeys(_symbol_code(item) for item in rows))
    if source == "watchlist":
        symbols = db.execute(
            select(Symbol)
            .join(
                WatchlistItem,
                WatchlistItem.symbol_id == Symbol.id,
            )
            .where(
                Symbol.asset_type == "stock",
                Symbol.is_active == 1,
            )
            .order_by(Symbol.id)
            .limit(limit)
        ).scalars().all()
    elif source == "positions":
        symbols = db.execute(
            select(Symbol)
            .join(Position, Position.symbol_id == Symbol.id)
            .where(
                Symbol.asset_type == "stock",
                Symbol.is_active == 1,
            )
            .order_by(Symbol.id)
            .limit(limit)
        ).scalars().all()
    else:
        raise ValueError(f"unsupported tail proxy source: {source}")
    return list(
        dict.fromkeys(
            _symbol_code(item.symbol)
            for item in symbols
            if region_from_market(item.market) == "cn"
        )
    )


def resolve_tail_proxy_trade_date(
    db: Session, requested_date: date | None = None
) -> date:
    """Resolve the latest usable trading date for an automatic snapshot.

    Scheduled jobs often run on weekends or exchange holidays.  Using
    ``date.today()`` directly makes the minute endpoint return an empty frame
    even though the most recent trading session is available locally.  The
    daily-bar table is the local trading-day calendar; use its latest date and
    only fall back to the previous weekday when the market database is empty.
    Explicit dates remain unchanged for replay/tests.
    """
    if requested_date is not None:
        return requested_date
    today = date.today()
    latest = db.scalar(
        select(func.max(DailyBar.trade_date)).where(DailyBar.trade_date <= today)
    )
    if latest is not None:
        return latest
    fallback = today
    while fallback.weekday() >= 5:
        fallback -= timedelta(days=1)
    return fallback


def _fetch_minute_frame(
    db: Session, *, symbol: str, trade_date: date
) -> pd.DataFrame:
    start = f"{trade_date.isoformat()} 09:30:00"
    end = f"{trade_date.isoformat()} 15:00:00"
    with _proxy_bypass(), quiet_akshare_output():
        frame = call_akshare_with_retry(
            ak.stock_zh_a_hist_min_em,
            symbol=symbol,
            start_date=start,
            end_date=end,
            period="1",
            adjust="",
            api_key=_API_KEY,
            db=db,
        )
    return frame if frame is not None else pd.DataFrame()


def sync_tail_proxy_snapshots(
    db: Session,
    *,
    source: str = "candidates",
    limit: int = 20,
    trade_date: date | None = None,
) -> TailProxySyncResult:
    target_date = resolve_tail_proxy_trade_date(db, trade_date)
    symbols = resolve_tail_proxy_symbols(db, source=source, limit=limit)
    written = 0
    skipped = 0
    failed = 0
    consecutive_failures = 0
    errors: list[str] = []
    for index, symbol in enumerate(symbols):
        try:
            frame = _fetch_minute_frame(
                db, symbol=symbol, trade_date=target_date
            )
            consecutive_failures = 0
            metrics = calculate_tail_proxy(
                frame, trade_date=target_date
            )
            if metrics is None:
                skipped += 1
                continue
            existing = db.execute(
                select(TailAccumulationSnapshot).where(
                    TailAccumulationSnapshot.symbol == symbol,
                    TailAccumulationSnapshot.trade_date == target_date,
                    TailAccumulationSnapshot.source == _SOURCE,
                )
            ).scalars().first()
            if existing is None:
                existing = TailAccumulationSnapshot(
                    symbol=symbol,
                    trade_date=target_date,
                    source=_SOURCE,
                )
                db.add(existing)
            payload = asdict(metrics)
            for field, value in payload.items():
                if field != "trade_date":
                    setattr(existing, field, value)
            existing.raw_json = frame.to_json(
                orient="records", force_ascii=False, date_format="iso"
            )
            written += 1
        except Exception as exc:
            failed += 1
            consecutive_failures += 1
            if len(errors) < 10:
                errors.append(f"{symbol}: {exc}")
            logger.warning(
                "Tail proxy sync failed for %s: %s", symbol, exc
            )
            if consecutive_failures >= 3:
                remaining = len(symbols) - index - 1
                failed += remaining
                if len(errors) < 10 and remaining:
                    errors.append(
                        "circuit_open:"
                        f"{remaining} symbols not requested after "
                        "3 consecutive failures"
                    )
                break
    db.flush()
    return TailProxySyncResult(
        trade_date=target_date,
        source_scope=source,
        total=len(symbols),
        written=written,
        skipped=skipped,
        failed=failed,
        errors=tuple(errors),
    )


__all__ = [
    "TailProxySyncResult",
    "resolve_tail_proxy_symbols",
    "resolve_tail_proxy_trade_date",
    "sync_tail_proxy_snapshots",
]
