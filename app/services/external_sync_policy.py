"""Exact date subtraction for external sync; bounds alone never prove coverage."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models.market_data import MarketCalendar
from app.services.sync_coverage import covered_dates, source_covered_dates

POLICIES = {"normal", "missing", "force"}


def sync_policy(payload: dict) -> str:
    value = str(payload.get("sync_policy", "normal"))
    if value not in POLICIES:
        raise ValueError("同步策略必须为 normal、missing 或 force")
    return value


def requested_days(db, dataset: str, start: date, end: date) -> tuple[date, ...]:
    """Announcement dates include closures; quote/event dates exclude them."""
    closed = set()
    if dataset != "financial":
        closed = set(db.scalars(select(MarketCalendar.trade_date).where(
            MarketCalendar.trade_date.between(start, end), MarketCalendar.is_trading_day == 0,
        )))
    days = []
    day = start
    while day <= end:
        if dataset == "financial" or (day.weekday() < 5 and day not in closed):
            days.append(day)
        day += timedelta(days=1)
    return tuple(days)


def subtract_ranges(days, covered) -> list[tuple[date, date]]:
    """Coalesce absent sessions, splitting at every covered session/interior hole."""
    result = []
    begin = last = None
    for day in days:
        if day in covered:
            if begin is not None:
                result.append((begin, last))
                begin = last = None
        elif begin is None:
            begin = last = day
        else:
            last = day
    if begin is not None:
        result.append((begin, last))
    return result


def resolve_policy_ranges(db, dataset: str, symbol_ids, start: date, end: date, policy: str):
    ids = list(dict.fromkeys(symbol_ids))
    days = requested_days(db, dataset, start, end)
    if policy == "force":
        return {sid: subtract_ranges(days, set()) for sid in ids}
    # Missing-only must inspect source data anew. A ledger point cannot hide a
    # row removed since the last successful scan, or a newly added report/event.
    if dataset in {"financial", "lhb", "hot_rank"}:
        covered = covered_dates(db, dataset, ids, start, end) if policy == "normal" else {sid: set() for sid in ids}
    else:
        if policy == "missing":
            covered = source_covered_dates(db, dataset, ids, start, end, require_complete=False)
        else:
            # Re-check current source evidence. An old final ledger point must
            # not mask a deleted row or a later forced intraday replacement.
            covered = source_covered_dates(db, dataset, ids, start, end, require_complete=True)
    return {sid: subtract_ranges(days, covered.get(sid, set())) for sid in ids}
