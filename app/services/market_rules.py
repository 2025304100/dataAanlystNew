"""A 股市场规则共享模块。

抽取自 sim_accounts.py 的 lot_size_for_symbol / normalize_order_quantity，
并新增 T+1 限制、涨跌停板、最小变动价位等真实市场规则。

供模拟交易、（未来的）自动下单、回测撮合三方共用，
确保模拟结果与真实市场行为一致。

设计原则：
- 纯函数为主，DB 查询函数明确标注 Session 参数
- 规则可配置（通过 rule_overrides 覆盖默认行为），支持组合级开关
- 失败时抛 HTTPException（API 层友好），但提供 is_* 系列纯判断函数供回测使用
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.portfolio import Position
from app.models.sim_account import SimTrade
from app.models.symbol import Symbol
from app.services.regions import region_from_market


# ----------------------------------------------------------------------------
# 手数规则
# ----------------------------------------------------------------------------

def lot_size_for_symbol(symbol: Symbol) -> int:
    """返回标的的最小交易手数。

    A 股：100 股为 1 手
    其他市场（美股/ETF 等）：1 股为 1 手
    """
    return 100 if region_from_market(symbol.market) == "cn" else 1


def normalize_order_quantity(symbol: Symbol, quantity: float) -> float:
    """将下单数量向下取整为手数的整数倍。

    例如 A 股下单 250 股 → 实际成交 200 股（2 手）
    """
    from math import floor
    lot_size = lot_size_for_symbol(symbol)
    normalized = floor(float(quantity) / lot_size) * lot_size
    return float(normalized)


# ----------------------------------------------------------------------------
# 最小变动价位
# ----------------------------------------------------------------------------

def min_tick_size(symbol: Symbol) -> float:
    """返回标的最小变动价位。

    A 股股票：0.01 元
    A 股 ETF：0.001 元
    美股：0.01 美元
    """
    region = region_from_market(symbol.market)
    if region == "cn":
        if symbol.asset_type == "etf":
            return 0.001
        return 0.01
    return 0.01


def round_to_tick(price: float, symbol: Symbol) -> float:
    """将价格圆整到最小变动价位。"""
    tick = min_tick_size(symbol)
    if tick <= 0:
        return float(price)
    return round(round(float(price) / tick) * tick, 4)


# ----------------------------------------------------------------------------
# 涨跌停板规则
# ----------------------------------------------------------------------------

# 涨跌停比例（按板块/类型）
# 主板/创业板/科创板：10%（创业板/科创板实际为 20%，此处按 board 区分）
# ST 股票：5%
# ETF：10%（部分 ETF 20%，简化处理）
LIMIT_UP_DOWN_PCT: dict[str, float] = {
    "main": 0.10,
    "chinext": 0.20,  # 创业板
    "star": 0.20,     # 科创板
    "st": 0.05,
    "etf": 0.10,
}


def _limit_pct_for_symbol(symbol: Symbol) -> float:
    """根据标的板块和 ST 标记返回涨跌停比例。"""
    if symbol.is_st:
        return LIMIT_UP_DOWN_PCT["st"]
    if symbol.asset_type == "etf":
        return LIMIT_UP_DOWN_PCT["etf"]
    board = (symbol.board or "").lower()
    if "chinext" in board or "创业板" in board:
        return LIMIT_UP_DOWN_PCT["chinext"]
    if "star" in board or "科创板" in board:
        return LIMIT_UP_DOWN_PCT["star"]
    return LIMIT_UP_DOWN_PCT["main"]


def compute_price_limits(prev_close: float, symbol: Symbol) -> tuple[float, float]:
    """根据前收盘价计算涨跌停价。

    Args:
        prev_close: 前一交易日收盘价
        symbol: 标的元数据

    Returns:
        (limit_up_price, limit_down_price)
    """
    if prev_close <= 0:
        return (0.0, 0.0)
    pct = _limit_pct_for_symbol(symbol)
    limit_up = round_to_tick(prev_close * (1.0 + pct), symbol)
    limit_down = round_to_tick(prev_close * (1.0 - pct), symbol)
    return (limit_up, limit_down)


def check_price_limit(
    fill_price: float,
    prev_close: float,
    symbol: Symbol,
    side: str,
) -> tuple[bool, str | None]:
    """检查成交价是否触及涨跌停（无法成交）。

    Args:
        fill_price: 拟成交价
        prev_close: 前收盘价
        symbol: 标的
        side: "buy" or "sell"

    Returns:
        (can_fill, reason)
        - 买入时，若 fill_price >= limit_up → 无法成交（涨停封板）
        - 卖出时，若 fill_price <= limit_down → 无法成交（跌停封板）
    """
    if prev_close <= 0:
        return (True, None)
    limit_up, limit_down = compute_price_limits(prev_close, symbol)
    if side == "buy" and fill_price >= limit_up:
        return (False, f"涨停封板（限价 {limit_up}），无法买入")
    if side == "sell" and fill_price <= limit_down:
        return (False, f"跌停封板（限价 {limit_down}），无法卖出")
    return (True, None)


def prev_close_for_symbol(db: Session, symbol_id: int, before_date: date | None = None) -> float | None:
    """查询标的的前收盘价。

    Args:
        db: 数据库会话
        symbol_id: 标的 ID
        before_date: 截止日期（不含），None 表示最新一条的前一条

    Returns:
        前收盘价；若无数据返回 None
    """
    stmt = select(DailyBar.close).where(DailyBar.symbol_id == symbol_id)
    if before_date is not None:
        stmt = stmt.where(DailyBar.trade_date < before_date)
    stmt = stmt.order_by(desc(DailyBar.trade_date), desc(DailyBar.id)).limit(1)
    return db.execute(stmt).scalar_one_or_none()


# ----------------------------------------------------------------------------
# T+1 规则
# ----------------------------------------------------------------------------

def check_t_plus_1(
    db: Session,
    portfolio_id: int,
    symbol_id: int,
    sell_quantity: float,
    today: date | None = None,
) -> tuple[bool, str | None]:
    """检查卖出是否违反 T+1 规则（当日买入当日不可卖）。

    A 股规则：T 日买入的股票，T+1 日才能卖出。
    通过查询今日买入的 SimTrade 数量，判断可卖余额是否充足。

    Args:
        db: 数据库会话
        portfolio_id: 组合 ID
        symbol_id: 标的 ID
        sell_quantity: 拟卖出数量
        today: 今日日期，None 时取 UTC 当前日期

    Returns:
        (can_sell, reason)
    """
    if today is None:
        today = datetime.now(timezone.utc).replace(tzinfo=None).date()

    # 查询今日买入的成交量
    today_buys = db.execute(
        select(SimTrade.quantity)
        .where(
            SimTrade.portfolio_id == portfolio_id,
            SimTrade.symbol_id == symbol_id,
            SimTrade.side == "buy",
        )
    ).scalars().all()

    # SimTrade 没有 trade_date 字段，用 created_at 日期匹配
    # 重新查询带 created_at 的完整记录
    today_buy_records = db.execute(
        select(SimTrade.quantity, SimTrade.created_at)
        .where(
            SimTrade.portfolio_id == portfolio_id,
            SimTrade.symbol_id == symbol_id,
            SimTrade.side == "buy",
        )
        .order_by(desc(SimTrade.id))
    ).all()

    today_bought_qty = 0.0
    for qty, created_at in today_buy_records:
        if created_at is None:
            continue
        # created_at 是 UTC，转日期比较
        trade_date = created_at.date() if hasattr(created_at, "date") else created_at
        if trade_date == today:
            today_bought_qty += float(qty or 0)

    if today_bought_qty <= 0:
        # 今日无买入，不触发 T+1 限制
        return (True, None)

    # 查询当前持仓总量
    position = db.execute(
        select(Position).where(
            Position.portfolio_id == portfolio_id,
            Position.symbol_id == symbol_id,
        )
    ).scalars().first()

    if position is None:
        return (False, "无持仓，无法卖出")

    held_qty = float(position.quantity or 0)
    # T+1 可卖 = 总持仓 - 今日买入
    sellable_qty = held_qty - today_bought_qty
    if sellable_qty < sell_quantity:
        return (False, f"T+1 限制：今日买入 {today_bought_qty} 股不可卖，可卖 {sellable_qty} 股")

    return (True, None)


# ----------------------------------------------------------------------------
# 综合校验入口
# ----------------------------------------------------------------------------

def validate_market_rules(
    db: Session,
    symbol: Symbol,
    side: str,
    quantity: float,
    fill_price: float,
    portfolio_id: int,
    *,
    enforce_t_plus_1: bool = True,
    enforce_price_limit: bool = True,
    today: date | None = None,
) -> None:
    """综合校验市场规则，违反时抛 HTTPException。

    供模拟交易下单前调用。自动下单场景应强制启用所有规则；
    手动下单可选择性禁用（向后兼容）。

    Args:
        db: 数据库会话
        symbol: 标的
        side: "buy" or "sell"
        quantity: 下单数量（已归一化为手数倍）
        fill_price: 拟成交价
        portfolio_id: 组合 ID
        enforce_t_plus_1: 是否启用 T+1 校验（默认 True）
        enforce_price_limit: 是否启用涨跌停校验（默认 True）
        today: 今日日期（测试注入用）

    Raises:
        HTTPException: 任何规则违反时抛 400
    """
    if enforce_price_limit:
        prev_close = prev_close_for_symbol(db, symbol.id, before_date=today)
        if prev_close is not None and prev_close > 0:
            can_fill, reason = check_price_limit(fill_price, prev_close, symbol, side)
            if not can_fill:
                raise HTTPException(status_code=400, detail=reason)

    if enforce_t_plus_1 and side == "sell":
        can_sell, reason = check_t_plus_1(db, portfolio_id, symbol.id, quantity, today=today)
        if not can_sell:
            raise HTTPException(status_code=400, detail=reason)
