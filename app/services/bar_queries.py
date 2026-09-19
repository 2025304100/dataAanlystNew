from __future__ import annotations

from datetime import date

from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol


MarketBar = DailyBar | UniverseDailyBar


def load_recent_bars(db: Session, symbol_id: int, limit: int = 60) -> list[MarketBar]:
    """Merge business and universe bars, preferring business rows on duplicate dates."""
    business_bars = db.execute(
        select(DailyBar)
        .where(DailyBar.symbol_id == symbol_id)
        .order_by(desc(DailyBar.trade_date))
        .limit(limit)
    ).scalars().all()

    universe_symbol_id = db.execute(
        select(UniverseSymbol.id)
        .join(Symbol, Symbol.symbol == UniverseSymbol.symbol)
        .where(Symbol.id == symbol_id)
    ).scalar_one_or_none()
    if universe_symbol_id is None:
        return list(reversed(business_bars))

    universe_bars = db.execute(
        select(UniverseDailyBar)
        .where(
            UniverseDailyBar.universe_symbol_id == universe_symbol_id,
            UniverseDailyBar.open.is_not(None),
            UniverseDailyBar.high.is_not(None),
            UniverseDailyBar.low.is_not(None),
            UniverseDailyBar.close.is_not(None),
        )
        .order_by(desc(UniverseDailyBar.trade_date))
        .limit(limit)
    ).scalars().all()
    merged = {bar.trade_date: bar for bar in universe_bars}
    merged.update({bar.trade_date: bar for bar in business_bars})
    return sorted(merged.values(), key=lambda bar: bar.trade_date)[-limit:]


def load_forward_bars_map(
    db: Session,
    start_dates: dict[int, date],
) -> dict[int, list[MarketBar]]:
    """Batch-load merged forward bars, filling stale business history from universe data."""
    if not start_dates:
        return {}

    symbol_ids = list(start_dates)
    business_map: dict[int, list[MarketBar]] = {symbol_id: [] for symbol_id in symbol_ids}
    business_bars = db.execute(
        select(DailyBar)
        .where(
            DailyBar.symbol_id.in_(symbol_ids),
            DailyBar.trade_date >= min(start_dates.values()),
        )
        .order_by(DailyBar.symbol_id.asc(), asc(DailyBar.trade_date))
    ).scalars().all()
    for bar in business_bars:
        if bar.trade_date >= start_dates[bar.symbol_id]:
            business_map[bar.symbol_id].append(bar)

    symbol_rows = db.execute(
        select(Symbol.id, UniverseSymbol.id)
        .join(UniverseSymbol, UniverseSymbol.symbol == Symbol.symbol)
        .where(Symbol.id.in_(symbol_ids))
    ).all()
    universe_to_symbol = {universe_id: symbol_id for symbol_id, universe_id in symbol_rows}
    if not universe_to_symbol:
        return business_map

    universe_bars = db.execute(
        select(UniverseDailyBar)
        .where(
            UniverseDailyBar.universe_symbol_id.in_(universe_to_symbol),
            UniverseDailyBar.trade_date >= min(start_dates.values()),
            UniverseDailyBar.open.is_not(None),
            UniverseDailyBar.high.is_not(None),
            UniverseDailyBar.low.is_not(None),
            UniverseDailyBar.close.is_not(None),
        )
        .order_by(UniverseDailyBar.universe_symbol_id.asc(), asc(UniverseDailyBar.trade_date))
    ).scalars().all()
    universe_map: dict[int, list[MarketBar]] = {symbol_id: [] for symbol_id in symbol_ids}
    for bar in universe_bars:
        symbol_id = universe_to_symbol[bar.universe_symbol_id]
        if bar.trade_date >= start_dates[symbol_id]:
            universe_map[symbol_id].append(bar)

    bars_map: dict[int, list[MarketBar]] = {}
    for symbol_id in symbol_ids:
        merged = {bar.trade_date: bar for bar in universe_map[symbol_id]}
        merged.update({bar.trade_date: bar for bar in business_map[symbol_id]})
        bars_map[symbol_id] = sorted(merged.values(), key=lambda bar: bar.trade_date)
    return bars_map
