from __future__ import annotations

import logging
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
# 共享模块：成本模型 + 市场规则。抽取自此文件原 lot_size/normalize_order_quantity，
# 并新增 T+1/涨跌停/手续费/滑点等真实市场规则，供模拟交易、（未来的）自动下单共用。
from app.services.cost_model import DEFAULT_COST_CONFIG, compute_cost, compute_fill_price_with_slippage
from app.services.market_rules import (
    lot_size_for_symbol,
    normalize_order_quantity,
    round_to_tick,
    validate_market_rules,
)
from app.services.portfolio_asset_scope import ensure_symbol_in_scope

logger = logging.getLogger(__name__)


def _round_money(value: float) -> float:
    return round(float(value), 2)


# 注：lot_size_for_symbol / normalize_order_quantity 已从 app.services.market_rules 导入，
# 此处保留导入以维持向后兼容（外部模块可能 from app.services.sim_accounts import lot_size_for_symbol）。


def latest_prices_for_symbols(db: Session, symbol_ids: list[int]) -> dict[int, float]:
    """Batch quotes by trading date; backfilled history can have a larger ID."""
    if not symbol_ids:
        return {}
    latest_dates = (
        select(DailyBar.symbol_id, func.max(DailyBar.trade_date).label('trade_date'))
        .where(DailyBar.symbol_id.in_(set(symbol_ids)))
        .group_by(DailyBar.symbol_id).subquery()
    )
    rows = db.execute(
        select(DailyBar.symbol_id, DailyBar.close).join(latest_dates,
            (DailyBar.symbol_id == latest_dates.c.symbol_id)
            & (DailyBar.trade_date == latest_dates.c.trade_date))
    ).all()
    # (symbol_id, trade_date) is unique in the production daily_bars contract.
    return {symbol_id: float(close) for symbol_id, close in rows}


def latest_price_for_symbol(db: Session, symbol_id: int) -> float | None:
    return latest_prices_for_symbols(db, [symbol_id]).get(symbol_id)


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
    *,
    enforce_rules: bool = True,
    apply_fees: bool = True,
    cost_config: dict | None = None,
    fee_override: float | None = None,
    execution_price_is_final: bool = False,
    skip_source_pending_gate: bool = False,
) -> tuple[SimOrder, SimTrade]:
    """下模拟订单。

    改造说明（P0）：
    - 默认启用真实手续费/滑点（apply_fees=True），修复原 fee=0.0 的失真问题
    - 默认启用市场规则校验（enforce_rules=True）：T+1 限制 + 涨跌停板
    - 通过 cost_config 可自定义费率（组合级覆盖）
    - 旧的 fee=0.0 行为可通过 apply_fees=False 恢复（仅供测试/兼容）

    Args:
        db: 数据库会话
        portfolio: 组合（必须 account_type="simulated"）
        symbol: 标的
        side: "buy" or "sell"
        quantity: 下单数量（自动归一化为手数倍）
        price: 指定价（None 时用最新收盘价）
        order_type: "market" or "limit"
        note: 订单备注
        enforce_rules: 是否强制市场规则（T+1/涨跌停），默认 True
        apply_fees: 是否计算真实手续费/滑点，默认 True
        cost_config: 自定义成本配置，None 时用 DEFAULT_COST_CONFIG
        fee_override: 统一撮合器已计算的费用；传入后跳过本地费用重算。
        execution_price_is_final: ``price`` 已由统一撮合器确定；跳过本地
            tick 取整和滑点，保留跨入口的精确成交价。
        skip_source_pending_gate: 仅限同一自动执行批次。批次开始时已校验
            账户来源，批次内逐单提交产生的收据不得反过来阻断后续买入。

    Returns:
        (SimOrder, SimTrade)
    """
    if portfolio.account_type != "simulated":
        raise HTTPException(status_code=400, detail="Only simulated portfolios support sim orders")
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="Order side must be buy or sell")
    if order_type not in {"market", "limit"}:
        raise HTTPException(status_code=400, detail="Order type must be market or limit")
    # A narrowed portfolio may still sell a historical out-of-scope holding,
    # but cannot open or add to one through the generic simulated-order API.
    if side == "buy":
        try:
            ensure_symbol_in_scope(portfolio, symbol)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 组合级串行化（R6）：同一组合的并发下单必须串行，避免读到过期持仓/现金而超卖或超支。
    # MySQL/InnoDB 通过 SELECT ... FOR UPDATE 行锁实现；SQLite 写事务本身串行、且该子句被
    # 方言忽略（等价空操作）。真并发一致性仍需 MySQL 现场实验验证。
    db.execute(
        select(Portfolio.id).where(Portfolio.id == portfolio.id).with_for_update()
    )

    if side == "buy":
        # Account coverage also bounds direct manual/AI simulated entries.
        # Risk exits retain their independent existing route.
        from zoneinfo import ZoneInfo
        from app.services.portfolio_reconciliation_activation import CoverageError, assert_execution_within_active_coverage
        try:
            assert_execution_within_active_coverage(db, portfolio.id, datetime.now(ZoneInfo("Asia/Shanghai")).date())
            if not skip_source_pending_gate:
                from app.services.portfolio_reconciliation_events import assert_account_sources_checked_for_buy
                assert_account_sources_checked_for_buy(db, portfolio.id)
        except CoverageError as exc:
            raise HTTPException(exc.status_code, detail={"error_code": exc.code, "message": str(exc)}) from exc
    ensure_sim_account_seed(db, portfolio)

    normalized_quantity = normalize_order_quantity(symbol, quantity)
    if normalized_quantity <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Quantity must be at least one lot ({lot_size_for_symbol(symbol)}) for this market",
        )

    latest_price = latest_price_for_symbol(db, symbol.id)
    raw_price = price if price and price > 0 else (latest_price or 0.0)
    base_price = float(raw_price) if execution_price_is_final else _round_money(raw_price)
    if base_price <= 0:
        raise HTTPException(status_code=400, detail={
            "error_code": "SIM_NO_USABLE_PRICE",
            "title_zh": "无可用价格",
            "detail_zh": f"标的 {symbol.symbol} 当前无可用价格（行情未镜像或价格为空），无法模拟成交。",
        })

    # 圆整到最小变动价位（A 股 0.01，ETF 0.001）
    if not execution_price_is_final:
        base_price = round_to_tick(base_price, symbol)

    # 应用滑点：买入成交价上浮，卖出成交价下浮（模拟真实撮合摩擦）
    if apply_fees:
        fill_price = _round_money(compute_fill_price_with_slippage(base_price, side, cost_config))
    else:
        fill_price = base_price

    # 计算真实手续费（佣金 + 印花税 + 滑点成本）
    if fee_override is not None:
        fee = _round_money(float(fee_override))
    elif apply_fees:
        fee = _round_money(compute_cost(
            fill_price, normalized_quantity, side, cost_config,
            asset_type=symbol.asset_type, market=symbol.market,
        ))
    else:
        fee = 0.0

    filled_amount = _round_money(normalized_quantity * fill_price)

    # 市场规则校验：T+1（卖出）+ 涨跌停板（买卖）
    # 在资金校验前执行，避免规则违反时仍扣款
    if enforce_rules:
        validate_market_rules(
            db=db,
            symbol=symbol,
            side=side,
            quantity=normalized_quantity,
            fill_price=fill_price,
            portfolio_id=portfolio.id,
            enforce_t_plus_1=True,
            enforce_price_limit=True,
        )

    if side == "buy" and cash_balance(db, portfolio.id) < filled_amount + fee:
        raise HTTPException(status_code=400, detail={
            "error_code": "INSUFFICIENT_SIM_CASH",
            "title_zh": "模拟现金不足",
            "detail_zh": (
                f"需要 {round(filled_amount + fee, 2)}（含费用），"
                f"可用现金 {cash_balance(db, portfolio.id)}。"
            ),
        })

    order = SimOrder(
        portfolio_id=portfolio.id,
        symbol_id=symbol.id,
        side=side,
        order_type=order_type,
        quantity=normalized_quantity,
        limit_price=price if order_type == "limit" else None,
        submitted_price=base_price,
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
    # WP-MSG.7：成交事件接入通知系统（不阻断主流程）
    try:
        from app.services.notifications.event_emitter import emit_trade_executed
        emit_trade_executed(
            db,
            trade_id=trade.id,
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            symbol=symbol.symbol,
            action=side,
            quantity=float(normalized_quantity),
            price=float(fill_price),
        )
    except Exception:
        logger.warning(
            "emit_trade_executed failed for portfolio=%s symbol=%s side=%s (non-blocking)",
            portfolio.id, symbol.symbol, side, exc_info=True,
        )
    if side == "sell" and realized_pnl is not None and realized_pnl < 0:
        try:
            from app.services.allocation import get_active_rule
            from app.services.notifications.event_emitter import emit_max_loss_warning

            rule = get_active_rule(db, portfolio.id)
            threshold_ratio = float(
                rule.max_loss_per_trade_pct if rule is not None else 0
            )
            if threshold_ratio > 1:
                threshold_ratio /= 100.0
            cost_basis = float(filled_amount) - float(realized_pnl)
            loss_ratio = (
                -float(realized_pnl) / cost_basis if cost_basis > 0 else 0.0
            )
            if threshold_ratio > 0 and loss_ratio >= threshold_ratio:
                emit_max_loss_warning(
                    db,
                    portfolio_id=portfolio.id,
                    source_id=trade.id,
                    symbol=symbol.symbol,
                    loss_pct=loss_ratio * 100,
                    threshold_pct=threshold_ratio * 100,
                    current_price=float(fill_price),
                    avg_cost=cost_basis / float(normalized_quantity),
                    realized_pnl=float(realized_pnl),
                    event_key=f"portfolio:max_loss_warning:trade:{trade.id}",
                )
        except Exception:
            logger.warning(
                "emit_max_loss_warning failed for portfolio=%s trade=%s (non-blocking)",
                portfolio.id,
                trade.id,
                exc_info=True,
            )
    return order, trade


def apply_sim_order_fill(
    db: Session,
    portfolio: Portfolio,
    symbol: Symbol,
    order: SimOrder,
    side: str,
    quantity: float,
    price: float,
    *,
    fee_override: float = 0.0,
    note: str | None = None,
) -> SimTrade:
    """Apply a later fill to an existing partial simulated order.

    ``client_order_key`` identifies the immutable order plan, so a retry must
    accumulate on the original ``SimOrder`` instead of creating a second
    order with a duplicate key.  Each fill remains an individual ``SimTrade``
    for cash/position reconciliation.
    """
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="Order side must be buy or sell")
    if order.id is None or order.portfolio_id != portfolio.id or order.symbol_id != symbol.id:
        raise ValueError("partial order does not belong to portfolio/symbol")
    if order.side != side:
        raise ValueError("partial order side mismatch")
    if order.status not in {"partial", "pending", "pending_retry"}:
        raise ValueError(f"order is not retryable: {order.status}")

    ensure_sim_account_seed(db, portfolio)
    normalized_quantity = normalize_order_quantity(symbol, quantity)
    if normalized_quantity <= 0:
        raise HTTPException(status_code=400, detail={
            "error_code": "VALIDATION_ERROR",
            "title_zh": "下单数量不足一手",
            "detail_zh": f"该市场最少需下单 {lot_size_for_symbol(symbol)} 股（一手）。",
        })
    fill_price = float(price)
    if fill_price <= 0:
        raise HTTPException(status_code=400, detail={
            "error_code": "SIM_NO_USABLE_PRICE",
            "title_zh": "无可用价格",
            "detail_zh": f"标的 {symbol.symbol} 补交价格无效，无法重试成交。",
        })
    fee = _round_money(float(fee_override))
    filled_amount = _round_money(normalized_quantity * fill_price)
    if side == "buy" and cash_balance(db, portfolio.id) < filled_amount + fee:
        raise HTTPException(status_code=400, detail={
            "error_code": "INSUFFICIENT_SIM_CASH",
            "title_zh": "模拟现金不足",
            "detail_zh": (
                f"重试成交需要 {round(filled_amount + fee, 2)}（含费用），"
                f"可用现金 {cash_balance(db, portfolio.id)}。"
            ),
        })

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
        note=note or f"Simulated retry {side} {symbol.symbol}",
    )

    previous_quantity = float(order.filled_quantity or 0.0)
    previous_price = float(order.filled_price or 0.0)
    total_quantity = previous_quantity + float(normalized_quantity)
    order.filled_quantity = total_quantity
    order.filled_amount = _round_money(float(order.filled_amount or 0.0) + filled_amount)
    order.fee = _round_money(float(order.fee or 0.0) + fee)
    order.filled_price = (
        (previous_price * previous_quantity + fill_price * normalized_quantity) / total_quantity
        if total_quantity > 0 else 0.0
    )
    requested_quantity = float(order.quantity or 0.0)
    if requested_quantity > 0 and total_quantity >= requested_quantity - 1e-9:
        order.filled_quantity = requested_quantity
        order.status = "filled"
        order.rejection_code = None
        order.rejection_detail = None
    else:
        order.status = "partial"
    order.filled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if note:
        order.note = note

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
    try:
        from app.services.notifications.event_emitter import emit_trade_executed

        emit_trade_executed(
            db,
            trade_id=trade.id,
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            symbol=symbol.symbol,
            action=side,
            quantity=float(normalized_quantity),
            price=float(fill_price),
        )
    except Exception:
        logger.warning(
            "emit_trade_executed failed for retry portfolio=%s symbol=%s side=%s",
            portfolio.id, symbol.symbol, side, exc_info=True,
        )
    return trade


def build_sim_account_summary(db: Session, portfolio: Portfolio, *, update_positions: bool = True) -> dict:
    """Value the account; read endpoints opt out of ORM position updates."""
    positions = db.execute(select(Position).where(Position.portfolio_id == portfolio.id)).scalars().all()

    # 风控加固：批量预加载每个 symbol 的最新 bar，避免循环内 N+1 查询
    position_symbol_ids = [p.symbol_id for p in positions]
    latest_price_map = latest_prices_for_symbols(db, position_symbol_ids)

    market_value = 0.0
    unrealized_pnl = 0.0
    for position in positions:
        latest_price = latest_price_map.get(position.symbol_id) or position.latest_price or position.avg_cost
        if latest_price is None:
            # 无法确定价格，跳过该持仓避免计算异常
            continue
        # Quotes may have 3+ decimals (e.g. ETFs). Round amounts, not the quote
        # before multiplication, so account totals agree with position details.
        latest_price = float(latest_price)
        avg_cost = position.avg_cost if position.avg_cost is not None else latest_price
        valued_amount = _round_money(position.quantity * latest_price)
        if update_positions:
            position.latest_price = latest_price
            position.market_value = valued_amount
            position.position_pct = round(valued_amount / portfolio.total_capital, 4) if portfolio.total_capital else 0.0
        market_value += valued_amount
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
