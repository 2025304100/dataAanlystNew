"""Local, explicitly scoped price/calendar adapter; never invent holiday bars."""
import math
from datetime import datetime, time, timedelta

from sqlalchemy import select, func

from app.models.daily_bar import DailyBar
from app.models.index_price import IndexPrice
from app.models.market_data import MarketCalendar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.market_history_contract import explicit_source_price_mode

CHINA = timedelta(hours=8)


class QuoteUnavailable(ValueError):
    pass


def load_bars(db, config, *, start=None, end=None, limit=500):
    quote_key = config.get("quote_key") or config["target_key"]
    if config.get("quote_type", "index" if config["target_type"] == "sector" else "symbol") == "index":
        model, condition = IndexPrice, IndexPrice.symbol == config["quote_key"]
    else:
        symbol_id = db.scalar(select(Symbol.id).where(Symbol.symbol == quote_key))
        universe_id = db.scalar(select(UniverseSymbol.id).where(UniverseSymbol.symbol == quote_key))
        business_query = select(func.max(DailyBar.trade_date)).where(DailyBar.symbol_id == symbol_id)
        universe_query = select(func.max(UniverseDailyBar.trade_date)).where(UniverseDailyBar.universe_symbol_id == universe_id)
        if end:
            business_query = business_query.where(DailyBar.trade_date <= end)
            universe_query = universe_query.where(UniverseDailyBar.trade_date <= end)
        business_latest = db.scalar(business_query) if symbol_id else None
        universe_latest = db.scalar(universe_query) if universe_id else None
        prefer_business = bool(business_latest and (not universe_latest or business_latest >= universe_latest))
        if business_latest and business_latest == universe_latest and config.get("adaptive_threshold"):
            business_count = db.scalar(select(func.count()).select_from(DailyBar).where(
                DailyBar.symbol_id == symbol_id, DailyBar.trade_date <= business_latest))
            universe_count = db.scalar(select(func.count()).select_from(UniverseDailyBar).where(
                UniverseDailyBar.universe_symbol_id == universe_id, UniverseDailyBar.trade_date <= universe_latest))
            prefer_business = business_count >= universe_count
        store = config.get("quote_store")
        if store == "daily" or (not store and prefer_business):
            model, condition = DailyBar, DailyBar.symbol_id == symbol_id
        else:
            if not universe_id:
                return []
            model, condition = UniverseDailyBar, UniverseDailyBar.universe_symbol_id == universe_id
    query = select(model).where(condition)
    if start:
        query = query.where(model.trade_date >= start)
    if end:
        query = query.where(model.trade_date <= end)
    return list(reversed(list(db.scalars(query.order_by(model.trade_date.desc()).limit(limit)))))


def valid_bar(bar):
    values = [getattr(bar, key, None) for key in ("open", "high", "low", "close")]
    return all(value is not None and math.isfinite(value) and value > 0 for value in values) and (
        bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high)


def _percentile(values, percentile):
    values = sorted(values)
    position = (len(values) - 1) * percentile / 100
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def quote_context(db, config, now):
    local = now + CHINA
    today_row = db.get(MarketCalendar, local.date())
    if today_row is None:
        raise QuoteUnavailable("缺少今天的真实交易日历，请先同步交易日历")
    if today_row.is_trading_day and time(9, 30) <= local.time() < time(15):
        raise QuoteUnavailable("盘中消息需分钟行情；日线提醒将在收盘后重新评估")
    cutoff = local.date() if local.time() >= time(15) else local.date() - timedelta(days=1)
    reference_day = db.scalar(select(MarketCalendar.trade_date).where(
        MarketCalendar.trade_date <= cutoff, MarketCalendar.is_trading_day == 1
    ).order_by(MarketCalendar.trade_date.desc()).limit(1))
    if reference_day is None or (local.date() - reference_day).days > 40:
        raise QuoteUnavailable("无法定位可信的最近收盘交易日")
    observation_dates = list(db.scalars(select(MarketCalendar.trade_date).where(
        MarketCalendar.trade_date > reference_day, MarketCalendar.is_trading_day == 1
    ).order_by(MarketCalendar.trade_date).limit(config["window_days"])))
    if len(observation_dates) != config["window_days"]:
        raise QuoteUnavailable("未来观察窗口的真实交易日历不完整")
    last_date = observation_dates[-1]
    calendar = list(db.scalars(select(MarketCalendar).where(
        MarketCalendar.trade_date.between(reference_day, last_date))))
    if len(calendar) != (last_date-reference_day).days+1:
        raise QuoteUnavailable("观察窗口内交易日历有缺口")
    bars = load_bars(db, config, end=reference_day, limit=500)
    if not bars or bars[-1].trade_date != reference_day or not valid_bar(bars[-1]):
        raise QuoteUnavailable("最近收盘行情缺失或无效，请先同步目标行情")
    reference = bars[-1]
    is_index = config.get("quote_type", "index" if config["target_type"] == "sector" else "symbol") == "index"
    if not is_index and explicit_source_price_mode(reference.source) != "qfq":
        raise QuoteUnavailable("个股行情未明确标记前复权口径，请在数据中心同步前复权行情")
    calendar_rows = list(db.scalars(select(MarketCalendar).where(
        MarketCalendar.trade_date.between(bars[0].trade_date, reference_day))))
    history_calendar = {row.trade_date for row in calendar_rows if row.is_trading_day}
    calendar_days = {row.trade_date for row in calendar_rows}
    moves = []
    h = config["window_days"]
    for index, start_bar in enumerate(bars[:-h]):
        window = bars[index+1:index+1+h]
        if not valid_bar(start_bar) or any(not valid_bar(bar) or bar.source != start_bar.source for bar in window):
            continue
        expected = sorted(day for day in history_calendar if start_bar.trade_date < day <= window[-1].trade_date)
        complete = all(start_bar.trade_date+timedelta(days=offset) in calendar_days
                       for offset in range((window[-1].trade_date-start_bar.trade_date).days+1))
        if not complete or expected != [bar.trade_date for bar in window]:
            continue
        moves.append(max(max(abs(bar.high/start_bar.close-1), abs(bar.low/start_bar.close-1))*100 for bar in window))
    threshold = config["min_move_pct"]
    if config["adaptive_threshold"]:
        if len(moves) < 60:
            raise QuoteUnavailable("自适应门槛需要至少60个完整历史窗口，可先同步历史行情或关闭自适应试用")
        threshold = max(threshold, _percentile(moves, config["volatility_percentile"]))
    return {"reference_date": reference_day.isoformat(), "reference_close": reference.close,
        "price_source": reference.source, "price_basis": "index" if is_index else "qfq",
        "quote_store": "index" if is_index else "daily" if isinstance(reference, DailyBar) else "universe",
        "observation_dates": [day.isoformat() for day in observation_dates],
        "threshold_pct": threshold, "history_windows": len(moves),
        "historical_baseline_rate": sum(value >= threshold for value in moves)/len(moves) if len(moves) >= 60 else None,
        "due_at": datetime.combine(last_date, time(15, 5)) - CHINA}
