"""Local A-share calendar checks; never probe providers to detect a holiday."""
from datetime import date, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import and_, extract, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.models.market_data import MarketCalendar


def is_cn_trading_day(db: Session, day: date) -> bool:
    if day.weekday() >= 5:
        return False
    entry = db.get(MarketCalendar, day)
    if entry is None:
        raise RuntimeError(f"交易日历缺少 {day.isoformat()}，请更新交易日历后重试；未请求网络数据")
    return entry.is_trading_day == 1


def read_cn_calendar() -> list[tuple[date, int]]:
    """Read the same local cache for analytics; never use the network here."""
    from app.db.session import SessionLocal

    try:
        with SessionLocal() as db:
            return list(db.execute(select(MarketCalendar.trade_date, MarketCalendar.is_trading_day)).tuples())
    except (RuntimeError, SQLAlchemyError):
        # Standalone warehouse tools may have no application DB initialized.
        return []


def cn_open_day_clause(db: Session, column):
    """Filter a SQL date column using the local cache, excluding weekends too."""
    weekday = (
        func.dayofweek(column).between(2, 6)
        if db.get_bind().dialect.name == "mysql"
        else extract("dow", column).between(1, 5)
    )
    closed = select(MarketCalendar.trade_date).where(
        MarketCalendar.trade_date == column, MarketCalendar.is_trading_day == 0,
    ).exists()
    return and_(weekday, ~closed)


def range_is_cn_closed(db: Session, start: date, end: date) -> bool:
    """Skip only a range proven closed; unknown history remains fetchable."""
    known = dict(db.execute(select(MarketCalendar.trade_date, MarketCalendar.is_trading_day).where(
        MarketCalendar.trade_date >= start, MarketCalendar.trade_date <= end,
    )).tuples().all())
    day = start
    while day <= end:
        if day.weekday() < 5 and known.get(day) != 0:
            return False
        day += timedelta(days=1)
    return True
