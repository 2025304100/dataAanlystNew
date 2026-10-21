"""Exact daily evidence shared by backtest preflight and execution."""
from datetime import date
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.security_status import SecurityStatusDaily
from app.models.symbol import Symbol
from app.services.bfg_trade_calendar_adapter import get_trading_days
from app.services.regions import region_from_market
from app.services.security_status.local_import import get_imported_execution_constraints_for_range
from app.services.security_status.provenance import is_verified_status_row


class BacktestHistoryUnavailable(ValueError):
    def __init__(self, code: str, message: str, missing: list[dict]):
        super().__init__(message)
        self.code = code
        self.missing = missing


def load_backtest_history(db: Session, symbol_ids: list[int], start_date: date, end_date: date,
                          *, trade_days: list[date] | None = None) -> dict[tuple[int, date], dict]:
    """Require every requested CN symbol-day, including dates with no price bar.

    Only a dated import with intact receipts can supply exchange price limits.
    Suspension/delisting records need no executable price bounds: they cannot fill.
    """
    symbols = list(db.scalars(select(Symbol).where(Symbol.id.in_(set(symbol_ids)))))
    cn = {symbol.id: symbol for symbol in symbols if region_from_market(symbol.market) == "cn"}
    if not cn:
        return {}
    days = trade_days if trade_days is not None else get_trading_days(start_date, end_date, session=db)
    if not days:
        raise BacktestHistoryUnavailable("TRADE_CALENDAR_UNAVAILABLE", "所选日期缺少交易日历资料，无法核对历史股票状态。", [])
    rows = {(row.symbol_id, row.trade_date): row for row in db.scalars(
        select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id.in_(cn),
            SecurityStatusDaily.trade_date >= start_date, SecurityStatusDaily.trade_date <= end_date)
        .execution_options(populate_existing=True))}
    constraints = get_imported_execution_constraints_for_range(db, list(cn), start_date, end_date)
    missing_status, missing_limits, result = [], [], {}
    for sid, symbol in cn.items():
        for day in days:
            key = (sid, day)
            row = rows.get(key)
            evidence = {"symbol_id": sid, "symbol": symbol.symbol, "trade_date": day.isoformat()}
            if row is None or not is_verified_status_row(row, day):
                missing_status.append(evidence)
                continue
            value = {"is_st": bool(row.is_st), "is_suspended": bool(row.is_suspended),
                     "is_listed": bool(row.is_listed), "is_delisting_period": bool(row.is_delisting_period)}
            if value["is_listed"] and not value["is_suspended"]:
                original = constraints.get(key)
                if original is None:
                    missing_limits.append(evidence)
                    continue
                value.update(original)
            result[key] = value
    if missing_status:
        first = missing_status[0]
        raise BacktestHistoryUnavailable("STATUS_DATA_UNAVAILABLE",
            f"缺少 {first['symbol']} 在 {first['trade_date']} 的可靠历史状态资料，共 {len(missing_status)} 个股票交易日。"
            "请先补齐当天停牌、ST 和上市状态，不能用今天的状态补过去。", missing_status)
    if missing_limits:
        first = missing_limits[0]
        raise BacktestHistoryUnavailable("PRICE_LIMIT_DATA_UNAVAILABLE",
            f"缺少 {first['symbol']} 在 {first['trade_date']} 的历史涨跌停资料，共 {len(missing_limits)} 个股票交易日。"
            "请导入原始逐日限价和报价单位；当日不设限制也须有原始记录，不能按当前状态估算。", missing_limits)
    return result


def historical_order_rejection(historical: dict, side: str, price: float, volume: float,
                                *, high: float, low: float) -> str | None:
    """Conservative daily-bar execution; never manufacture an executable price."""
    if historical["is_suspended"] or not historical["is_listed"] or volume <= 0:
        return "HALTED"
    if side == "BUY" and (historical["is_st"] or historical["is_delisting_period"]):
        return "ST_BUY_BLOCKED" if historical["is_st"] else "DELISTING_BUY_BLOCKED"
    if not math.isfinite(price) or price <= 0:
        return "PRICE_INVALID"
    up, down = historical.get("upper_limit_price"), historical.get("lower_limit_price")
    if (up is not None and price > up + 1e-8) or (down is not None and price < down - 1e-8):
        return "PRICE_OUTSIDE_HISTORICAL_LIMITS"
    if side == "BUY" and up is not None and price >= up - 1e-8 and high <= up + 1e-8:
        return "LIMIT_UP_LOCKED"
    if side == "SELL" and down is not None and price <= down + 1e-8 and low >= down - 1e-8:
        return "LIMIT_DOWN_LOCKED"
    return None
