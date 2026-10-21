"""Explicit synthetic daily evidence for isolated backtest regression scenarios.

These tests assume normal listings and no exchange price cap unless supplied.
Do not enable this automatically: missing-history tests must retain their gaps.
"""
from datetime import date, datetime, time, timezone

from app.models.market_data import MarketCalendar
from app.models.symbol import Symbol
from app.schemas.security_status import SecurityStatusFileRequest
from app.services.security_status.local_import import import_file, preview_import


def seed_daily_history(db, symbol_id: int, days, *, limits_by_day=None):
    symbol = db.get(Symbol, symbol_id)
    normalized = sorted(set(days))
    if not normalized:
        return
    lines = []
    for day in normalized:
        if db.get(MarketCalendar, day) is None:
            db.add(MarketCalendar(trade_date=day, is_trading_day=1))
        bounds = (limits_by_day or {}).get(day, "NO_LIMIT,,,0.01")
        lines.append(f"{symbol.symbol},{day},0,0,0,1,1990-01-01,,{bounds}\n")
    db.flush()
    payload = SecurityStatusFileRequest(
        csv_text="symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,"
                 "price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n" + "".join(lines),
        source_name="regression_archive", source_reference="memory://isolated-scenario/daily-history",
        source_version="scenario-v1", source_updated_at=datetime.combine(normalized[-1], time(18), timezone.utc))
    preview = preview_import(db, payload)
    assert preview["can_import"], preview
    import_file(db, payload, preview_sha256=preview["content_sha256"])
    db.flush()
