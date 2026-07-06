from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import floor

from fastapi import HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.regions import region_from_market


def _round_money(value: float) -> float:
    return round(float(value), 2)


def lot_size_for_symbol(symbol: Symbol) -> int:
    return 100 if region_from_market(symbol.market) == "cn" else 1


def normalize_order_quantity(symbol: Symbol, quantity: float) -> float:
    lot_size = lot_size_for_symbol(symbol)
    normalized = floor(float(quantity) / lot_size) * lot_size
    return float(normalized)


def latest_price_for_symbol(db: Session, symbol_id: int) -> float | None:
    bar = db.execute(
        select(DailyBar).where(DailyBar.symbol_id == symbol_id).order_by(desc(DailyBar.trade_date), desc(DailyBar.id))
    ).scalars().first()
    if bar is None:
        return None
    return float(bar.close)


def ensure_sim_account_seed(db: Session, portfolio: Portfolio) -> None:
    if portfolio.account_type != "simulated":
        return
    existing = db.execute(
        select(CashLedger.id).where(CashLedger.portfolio_id == portfolio.id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return

    opening_balance = _round_money(portfolio.total_capital)
    db.add(
        CashLedger(
            portfolio_id=portfolio.id,
            entry_type="deposit",
            amount=opening_balance,
            balance_after=opening_balance,
            ref_type="portfolio",
            ref_id=portfolio.id,
            note="Initial simulated funding",
        )
    )
    db.flush()


def cash_balance(db: Session, portfolio_id: int) -> float:
    balance = db.execute(
        select(CashLedger.balance_after)
        .where(CashLedger.portfolio_id == portfolio_id)
        .order_by(desc(CashLedger.id))
        .limit(1)
    ).scalar_one_or_none()
    return _round_money(balance or 0.0)


def append_cash_ledger(
    db: Session,
    portfolio_id: int,
    entry_type: str,
    amount: float,
    ref_type: str | None = None,
    ref_id: int | None = None,
    note: str | None = None,
) -> CashLedger:
    next_balance = _round_money(cash_balance(db, portfolio_id) + amount)
    entry = CashLedger(
        portfolio_id=portfolio_id,
        entry_type=entry_type,
        amount=_round_money(amount),
        balance_after=next_balance,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
    )
    db.add(entry)
    db.flush()
    return entry


def _upsert_position(
    db: Session,
    portfolio: Portfolio,
    symbol: Symbol,
    side: str,
    quantity: float,
    fill_price: float,
) -> tuple[Position | None, float | None]:
    position = db.execute(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.symbol_id == symbol.id)
    ).scalars().first()

    realized_pnl = None
    if side == "buy":
        if position is None:
            position = Position(
                portfolio_id=portfolio.id,
                symbol_id=symbol.id,
                asset_type=symbol.asset_type,
                theme=symbol.theme,
                opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add(position)
            previous_quantity = 0.0
            previous_cost = 0.0
        else:
            previous_quantity = float(position.quantity)
            previous_cost = float(position.avg_cost) if position.avg_cost is not None else 0.0
        new_quantity = previous_quantity + quantity
        new_avg_cost = ((previous_quantity * previous_cost) + (quantity * fill_price)) / new_quantity if new_quantity else 0.0
        position.quantity = new_quantity
        position.avg_cost = _round_money(new_avg_cost)
        position.latest_price = _round_money(fill_price)
        position.market_value = _round_money(new_quantity * fill_price)
        position.position_pct = round(position.market_value / portfolio.total_capital, 4) if portfolio.total_capital else 0.0
        position.asset_type = symbol.asset_type
        position.theme = symbol.theme
        return position, realized_pnl

    if position is None or position.quantity < quantity:
        raise HTTPException(status_code=400, detail="Not enough position to sell")

    # 风控：avg_cost 可能为 NULL（旧数据/手动写入），用 fill_price 兜底避免 TypeError
    avg_cost = position.avg_cost if position.avg_cost is not None else fill_price
    realized_pnl = _round_money((fill_price - avg_cost) * quantity)
    remaining_quantity = float(position.quantity) - quantity
    if remaining_quantity <= 0:
        db.delete(position)
        return None, realized_pnl

    position.quantity = remaining_quantity
    position.latest_price = _round_money(fill_price)
    position.market_value = _round_money(remaining_quantity * fill_price)
    position.position_pct = round(position.market_value / portfolio.total_capital, 4) if portfolio.total_capital else 0.0
    return position, realized_pnl


def place_sim_order(
    db: Session,
    portfolio: Portfolio,
    symbol: Symbol,
    side: str,
    quantity: float,
    price: float | None = None,
    order_type: str = "market",
    note: str | None = None,
) -> tuple[SimOrder, SimTrade]:
    if portfolio.account_type != "simulated":
        raise HTTPException(status_code=400, detail="Only simulated portfolios support sim orders")
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="Order side must be buy or sell")
    if order_type not in {"market", "limit"}:
        raise HTTPException(status_code=400, detail="Order type must be market or limit")

    ensure_sim_account_seed(db, portfolio)

    normalized_quantity = normalize_order_quantity(symbol, quantity)
    if normalized_quantity <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Quantity must be at least one lot ({lot_size_for_symbol(symbol)}) for this market",
        )

    latest_price = latest_price_for_symbol(db, symbol.id)
    fill_price = _round_money(price if price and price > 0 else (latest_price or 0.0))
    if fill_price <= 0:
        raise HTTPException(status_code=400, detail="No usable price for simulated fill")

    filled_amount = _round_money(normalized_quantity * fill_price)
    fee = 0.0

    if side == "buy" and cash_balance(db, portfolio.id) < filled_amount + fee:
        raise HTTPException(status_code=400, detail="Not enough simulated cash")

    order = SimOrder(
        portfolio_id=portfolio.id,
        symbol_id=symbol.id,
        side=side,
        order_type=order_type,
        quantity=normalized_quantity,
        limit_price=price if order_type == "limit" else None,
        submitted_price=fill_price,
        status="filled",
        filled_quantity=normalized_quantity,
        filled_price=fill_price,
        filled_amount=filled_amount,
        fee=fee,
        note=note,
        filled_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(order)
    db.flush()

    _, realized_pnl = _upsert_position(
        db=db,
        portfolio=portfolio,
        symbol=symbol,
        side=side,
        quantity=normalized_quantity,
        fill_price=fill_price,
    )

    cash_delta = -(filled_amount + fee) if side == "buy" else (filled_amount - fee)
    append_cash_ledger(
        db=db,
        portfolio_id=portfolio.id,
        entry_type=side,
        amount=cash_delta,
        ref_type="sim_order",
        ref_id=order.id,
        note=note or f"Simulated {side} {symbol.symbol}",
    )

    trade = SimTrade(
        portfolio_id=portfolio.id,
        symbol_id=symbol.id,
        order_id=order.id,
        side=side,
        quantity=normalized_quantity,
        price=fill_price,
        amount=filled_amount,
        fee=fee,
        realized_pnl=realized_pnl,
        note=note,
    )
    db.add(trade)
    db.flush()
    return order, trade


def build_sim_account_summary(db: Session, portfolio: Portfolio) -> dict:
    positions = db.execute(select(Position).where(Position.portfolio_id == portfolio.id)).scalars().all()

    # 风控加固：批量预加载每个 symbol 的最新 bar，避免循环内 N+1 查询
    position_symbol_ids = [p.symbol_id for p in positions]
    latest_price_map: dict[int, float] = {}
    if position_symbol_ids:
        # 子查询：每个 symbol 的最大 id（即最新一条 bar）
        latest_bar_subq = (
            select(func.max(DailyBar.id))
            .where(DailyBar.symbol_id.in_(position_symbol_ids))
            .group_by(DailyBar.symbol_id)
            .scalar_subquery()
        )
        latest_bars = db.execute(
            select(DailyBar.symbol_id, DailyBar.close)
            .where(DailyBar.id.in_(latest_bar_subq))
        ).all()
        for sym_id, close in latest_bars:
            latest_price_map[sym_id] = float(close)

    market_value = 0.0
    unrealized_pnl = 0.0
    for position in positions:
        latest_price = latest_price_map.get(position.symbol_id) or position.latest_price or position.avg_cost
        if latest_price is None:
            # 无法确定价格，跳过该持仓避免计算异常
            continue
        latest_price = _round_money(latest_price)
        avg_cost = position.avg_cost if position.avg_cost is not None else latest_price
        position.latest_price = latest_price
        position.market_value = _round_money(position.quantity * latest_price)
        position.position_pct = round(position.market_value / portfolio.total_capital, 4) if portfolio.total_capital else 0.0
        market_value += position.market_value
        unrealized_pnl += (latest_price - avg_cost) * position.quantity

    realized_pnl = db.execute(
        select(func.coalesce(func.sum(SimTrade.realized_pnl), 0.0)).where(
            SimTrade.portfolio_id == portfolio.id,
            SimTrade.realized_pnl.is_not(None),
        )
    ).scalar_one()

    trades_7d = db.execute(
        select(func.count(SimTrade.id)).where(
            SimTrade.portfolio_id == portfolio.id,
            SimTrade.created_at >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7),
        )
    ).scalar_one()
    last_trade_at = db.execute(
        select(SimTrade.created_at).where(SimTrade.portfolio_id == portfolio.id).order_by(desc(SimTrade.id)).limit(1)
    ).scalar_one_or_none()

    has_ledger = db.execute(
        select(CashLedger.id).where(CashLedger.portfolio_id == portfolio.id).limit(1)
    ).scalar_one_or_none()
    cash = cash_balance(db, portfolio.id) if has_ledger is not None else _round_money(portfolio.total_capital)
    market_value = _round_money(market_value)
    unrealized_pnl = _round_money(unrealized_pnl)
    realized_pnl = _round_money(realized_pnl or 0.0)
    total_equity = _round_money(cash + market_value)
    available_cash = max(0.0, cash)

    return {
        "cash_balance": cash,
        "available_cash": _round_money(available_cash),
        "market_value": market_value,
        "total_equity": total_equity,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "cash_pct": round(cash / total_equity, 4) if total_equity else 0.0,
        "invested_pct": round(market_value / total_equity, 4) if total_equity else 0.0,
        "position_count": len(positions),
        "trade_count_7d": int(trades_7d or 0),
        "last_trade_at": last_trade_at,
    }


def recent_sim_trades(db: Session, portfolio_id: int, limit: int = 8, symbol_id: int | None = None) -> list[dict]:
    stmt = (
        select(SimTrade, Symbol)
        .join(Symbol, Symbol.id == SimTrade.symbol_id)
        .where(SimTrade.portfolio_id == portfolio_id)
        .order_by(desc(SimTrade.id))
        .limit(limit)
    )
    if symbol_id is not None:
        stmt = stmt.where(SimTrade.symbol_id == symbol_id)

    rows = db.execute(stmt).all()
    return [
        {
            "id": trade.id,
            "symbol_id": trade.symbol_id,
            "symbol": symbol.symbol,
            "name": symbol.name,
            "side": trade.side,
            "quantity": trade.quantity,
            "price": trade.price,
            "amount": trade.amount,
            "fee": trade.fee,
            "realized_pnl": trade.realized_pnl,
            "created_at": trade.created_at,
        }
        for trade, symbol in rows
    ]
