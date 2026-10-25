"""Read-only, independent dimensions for the market-data dashboard.

At least one stored bar, freshness, listing metadata and verified historical
coverage are separate facts. No registration flag certifies the other facts.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models.market_data import MarketCalendar
from app.models.universe import UniverseDailyBar, UniverseSymbol


def coverage_percent(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    if count == total:
        return 100.0
    # Never promote an incomplete numerator to 100 through rounding.
    return min(99.99, max(0.0, round(count * 100 / total, 2)))


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None


def read_universe_quality(db, *, now: datetime | None = None) -> dict:
    local = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    local = local.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if local.tzinfo is None else local.astimezone(ZoneInfo("Asia/Shanghai"))
    cutoff = local.date() - timedelta(days=1) if local.hour < 15 else local.date()
    with db.no_autoflush:
        calendar = dict(db.execute(select(MarketCalendar.trade_date, MarketCalendar.is_trading_day).where(
            MarketCalendar.trade_date.between(cutoff - timedelta(days=40), cutoff))).all())
        records = db.execute(select(UniverseSymbol.id, UniverseSymbol.symbol, UniverseSymbol.name,
            UniverseSymbol.region, UniverseSymbol.asset_type, UniverseSymbol.is_synced,
            UniverseSymbol.bar_count, UniverseSymbol.last_bar_date, UniverseSymbol.listed_at).order_by(UniverseSymbol.id)).all()
        bars = {symbol_id: (int(count), _day(latest)) for symbol_id, count, latest in db.execute(select(
            UniverseDailyBar.universe_symbol_id, func.count(UniverseDailyBar.id), func.max(UniverseDailyBar.trade_date)
        ).group_by(UniverseDailyBar.universe_symbol_id)).all()}
    expected = None
    day = cutoff
    while day in calendar and calendar[day] in (0, 1):
        if calendar[day] == 1:
            expected = day
            break
        day -= timedelta(days=1)
    groups = {}
    missing_samples, cache_samples = [], []
    registered = set()
    for row in records:
        key = f"{row.region}-{row.asset_type}"
        group = groups.setdefault(key, {"total": 0, "with_bars": 0, "without_bars": 0,
            "sync_flag_count": 0, "sync_flag_without_bars": 0, "cache_mismatches": 0,
            "listing_date_recorded": 0, "listing_date_missing": 0, "listing_date_invalid": 0,
            "latest_bar_date": None, "expected_latest_date": expected if row.region == "cn" else None,
            "calendar_status": ("verified" if expected else "unverified") if row.region == "cn" else "unverified",
            "freshness": {"current": 0, "earlier": 0, "unverified": 0, "no_bars": 0},
            "future_bar_dates": 0})
        registered.add(row.id)
        count, latest = bars.get(row.id, (0, None))
        group["total"] += 1
        group["sync_flag_count"] += row.is_synced == 1
        market_today = local.astimezone(ZoneInfo("America/New_York")).date() if row.region == "us" else local.date()
        listed = _day(row.listed_at)
        if row.listed_at is None:
            group["listing_date_missing"] += 1
        elif listed is None or listed > market_today:
            group["listing_date_invalid"] += 1
        else:
            group["listing_date_recorded"] += 1
        if count:
            group["with_bars"] += 1
            if latest and (group["latest_bar_date"] is None or latest > group["latest_bar_date"]):
                group["latest_bar_date"] = latest
            target = group["expected_latest_date"]
            if latest is None or latest > market_today or target is None or latest > target:
                group["freshness"]["unverified"] += 1
            elif latest == target:
                group["freshness"]["current"] += 1
            else:
                group["freshness"]["earlier"] += 1
            group["future_bar_dates"] += bool(latest and latest > market_today)
        else:
            group["without_bars"] += 1
            group["freshness"]["no_bars"] += 1
            group["sync_flag_without_bars"] += row.is_synced == 1
            if len(missing_samples) < 20:
                missing_samples.append({"id": row.id, "symbol": row.symbol, "name": row.name, "scope": key})
        if row.bar_count != count or _day(row.last_bar_date) != latest:
            group["cache_mismatches"] += 1
            if len(cache_samples) < 20:
                cache_samples.append({"symbol": row.symbol, "scope": key,
                    "cached_count": row.bar_count, "actual_count": count,
                    "cached_latest": row.last_bar_date, "actual_latest": latest})
    for group in groups.values():
        group["coverage_pct"] = coverage_percent(group["with_bars"], group["total"])
        group["listing_date_pct"] = coverage_percent(group["listing_date_recorded"], group["total"])
    return {"as_of": local.isoformat(), "timezone": "Asia/Shanghai", "basis": "stored_bars_by_symbol",
        "scopes": groups, "stored_bar_rows": sum(count for count, _ in bars.values()),
        "with_bars": sum(g["with_bars"] for g in groups.values()),
        "without_bars": sum(g["without_bars"] for g in groups.values()),
        "cache_mismatches": sum(g["cache_mismatches"] for g in groups.values()),
        "orphan_bar_rows": sum(count for key, (count, _) in bars.items() if key not in registered),
        "missing_bar_samples": missing_samples, "cache_mismatch_samples": cache_samples,
        "historical_calendar_coverage": "not_verified", "price_quantity_basis": "not_verified",
        "semantics": "one_stored_bar_is_not_complete_history_or_metadata"}
