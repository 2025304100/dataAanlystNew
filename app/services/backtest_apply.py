"""回测结果应用到模拟组合服务（P2-1）。

将 BacktestRun 的每笔 BacktestTrade 转换为 SimOrder + SimTrade，
按交易日期顺序重放，重建组合的持仓、现金、交易历史。

设计要点：
- 时间戳使用 entry_date/exit_date（A股收盘 15:00），避免污染最近交易统计
- 复用 sim_accounts._upsert_position 更新持仓，保证 avg_cost/realized_pnl 算法一致
- 复用回测中已计算好的 entry_cost/exit_cost 作为 fee，不重新计算滑点
- 单笔失败隔离：try/except 包住每笔，错误记录到 errors，不影响其他笔
- clear_existing 控制是否清空目标组合现有数据；默认 False，已有数据时拒绝应用

应用后的组合状态：
- 持仓：未平仓的 BacktestTrade（exit_date is None）会成为当前 Position
- 现金：initial_capital - 所有买入总花费 + 所有卖出总收入
- realized_pnl：来自 _upsert_position 卖出时计算（基于 avg_cost）
- 交易历史：所有买入+卖出都会出现在 recent_sim_trades 中
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestRun, BacktestTrade
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.sim_accounts import (
    _round_money,
    _upsert_position,
    cash_balance,
)

logger = logging.getLogger(__name__)

# A股收盘时间，用作历史回测成交的时间戳（naive datetime）
_TRADE_CLOSE_TIME = time(15, 0, 0)


def _date_to_datetime(d: date) -> datetime:
    """将 date 转为 naive datetime（A股收盘价时间）。"""
    return datetime.combine(d, _TRADE_CLOSE_TIME)


def _clear_portfolio_sim_data(db: Session, portfolio_id: int) -> None:
    """清空目标组合的所有模拟交易数据。

    删除顺序：SimTrade -> SimOrder -> Position -> CashLedger
    （SimTrade.order_id 有外键，需先于 SimOrder 删除）
    """
    for model in (SimTrade, SimOrder, Position, CashLedger):
        rows = db.execute(
            select(model).where(model.portfolio_id == portfolio_id)
        ).scalars().all()
        for row in rows:
            db.delete(row)
    db.flush()


def apply_backtest_run_to_portfolio(
    db: Session,
    run_id: int,
    portfolio_id: int,
    *,
    clear_existing: bool = False,
) -> dict[str, Any]:
    """将回测结果应用到模拟组合。

    将 BacktestRun 的每笔 BacktestTrade 转换为 SimOrder + SimTrade，
    按交易日期顺序重放，重建组合的持仓、现金、交易历史。

    Args:
        db: 数据库会话
        run_id: BacktestRun.id（必须 status="success"）
        portfolio_id: 目标组合 ID（必须 account_type="simulated"）
        clear_existing: 是否清空目标组合现有的 SimOrder/SimTrade/Position/CashLedger。
                       默认 False。若 False 且目标组合已有 SimOrder，将抛出 ValueError。

    Returns:
        {
            "run_id": int,
            "portfolio_id": int,
            "initial_capital": float,
            "final_cash": float,
            "applied_trades": int,  # 成功应用的 trade 事件数（买入+卖出分别计数）
            "skipped_trades": int,  # 跳过的 trade 事件数
            "open_positions": int,  # 应用后剩余持仓数（未平仓）
            "errors": list[str],  # 单笔失败的原因
        }

    Raises:
        ValueError: 回测不存在/未成功、组合不存在/非模拟、已有数据但未指定 clear_existing
    """
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise ValueError(f"BacktestRun {run_id} not found")
    if run.status != "success":
        raise ValueError(f"BacktestRun {run_id} status is {run.status}, not success")

    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    if portfolio.account_type != "simulated":
        raise ValueError(f"Portfolio {portfolio_id} is not simulated")

    # 检查目标组合是否已有 sim orders
    existing_order_id = db.execute(
        select(SimOrder.id).where(SimOrder.portfolio_id == portfolio_id).limit(1)
    ).scalar_one_or_none()
    if existing_order_id is not None and not clear_existing:
        raise ValueError(
            f"Portfolio {portfolio_id} already has sim orders. "
            "Set clear_existing=True to overwrite."
        )

    # 加载所有 trades，按 entry_date 升序
    trades = db.execute(
        select(BacktestTrade)
        .where(BacktestTrade.run_id == run_id)
        .order_by(BacktestTrade.entry_date, BacktestTrade.id)
    ).scalars().all()

    initial_capital = _round_money(float(run.initial_capital))

    if not trades:
        # 无 trades：仍清空 + 写入初始资金
        if clear_existing:
            _clear_portfolio_sim_data(db, portfolio_id)
        db.add(CashLedger(
            portfolio_id=portfolio_id,
            entry_type="deposit",
            amount=initial_capital,
            balance_after=initial_capital,
            ref_type="backtest_run",
            ref_id=run_id,
            note=f"Apply backtest run #{run_id} initial capital (no trades)",
        ))
        db.commit()
        return {
            "run_id": run_id,
            "portfolio_id": portfolio_id,
            "initial_capital": initial_capital,
            "final_cash": initial_capital,
            "applied_trades": 0,
            "skipped_trades": 0,
            "open_positions": 0,
            "errors": [],
        }

    # 清空目标组合现有数据
    if clear_existing:
        _clear_portfolio_sim_data(db, portfolio_id)

    # 重置初始资金：写入一笔 deposit
    db.add(CashLedger(
        portfolio_id=portfolio_id,
        entry_type="deposit",
        amount=initial_capital,
        balance_after=initial_capital,
        ref_type="backtest_run",
        ref_id=run_id,
        note=f"Apply backtest run #{run_id} initial capital",
    ))
    db.flush()

    applied = 0
    skipped = 0
    errors: list[str] = []

    # 预加载 symbol map
    symbol_ids = {t.symbol_id for t in trades}
    symbols = db.execute(
        select(Symbol).where(Symbol.id.in_(symbol_ids))
    ).scalars().all()
    symbol_map = {s.id: s for s in symbols}

    # 按时间顺序处理每笔 trade
    # 同一 trade 拆为 buy + sell 两个事件，按 (date, buy_first) 排序
    events: list[tuple[date, int, str, BacktestTrade]] = []
    for trade in trades:
        events.append((trade.entry_date, 0, "buy", trade))
        if trade.exit_date is not None:
            events.append((trade.exit_date, 1, "sell", trade))
    events.sort(key=lambda e: (e[0], e[1]))

    for event_date, _, side, trade in events:
        symbol = symbol_map.get(trade.symbol_id)
        if symbol is None:
            msg = f"Symbol {trade.symbol_id} not found, skipped {side} on {event_date}"
            errors.append(msg)
            skipped += 1
            continue

        try:
            if side == "buy":
                fill_price = _round_money(float(trade.entry_price))
                fee = _round_money(float(trade.entry_cost or 0))
                quantity = float(trade.quantity)
                event_note = f"Backtest #{run_id} buy {symbol.symbol} on {event_date}"
                event_dt = _date_to_datetime(trade.entry_date)
            else:  # sell
                fill_price = _round_money(float(trade.exit_price))
                fee = _round_money(float(trade.exit_cost or 0))
                quantity = float(trade.quantity)
                exit_reason = trade.exit_reason or ""
                event_note = f"Backtest #{run_id} sell {symbol.symbol} on {event_date} ({exit_reason})"
                event_dt = _date_to_datetime(trade.exit_date)

            if fill_price <= 0 or quantity <= 0:
                msg = f"Invalid price/quantity for {side} {symbol.symbol} on {event_date}"
                errors.append(msg)
                skipped += 1
                continue

            filled_amount = _round_money(quantity * fill_price)

            # 创建 SimOrder
            order = SimOrder(
                portfolio_id=portfolio_id,
                symbol_id=trade.symbol_id,
                side=side,
                order_type="limit",
                quantity=quantity,
                limit_price=fill_price,
                submitted_price=fill_price,
                status="filled",
                filled_quantity=quantity,
                filled_price=fill_price,
                filled_amount=filled_amount,
                fee=fee,
                note=event_note,
                created_at=event_dt,
                filled_at=event_dt,
            )
            db.add(order)
            db.flush()

            # 复用 _upsert_position 更新持仓（自动处理 avg_cost/realized_pnl）
            _, realized_pnl = _upsert_position(
                db=db,
                portfolio=portfolio,
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=fill_price,
            )

            # 写入 CashLedger（手动构造以使用历史时间戳）
            cash_delta = -(filled_amount + fee) if side == "buy" else (filled_amount - fee)
            next_balance = _round_money(cash_balance(db, portfolio_id) + cash_delta)
            db.add(CashLedger(
                portfolio_id=portfolio_id,
                entry_type=side,
                amount=_round_money(cash_delta),
                balance_after=next_balance,
                ref_type="sim_order",
                ref_id=order.id,
                note=event_note,
                created_at=event_dt,
            ))
            db.flush()

            # 创建 SimTrade
            db.add(SimTrade(
                portfolio_id=portfolio_id,
                symbol_id=trade.symbol_id,
                order_id=order.id,
                side=side,
                quantity=quantity,
                price=fill_price,
                amount=filled_amount,
                fee=fee,
                realized_pnl=realized_pnl,
                note=event_note,
                created_at=event_dt,
            ))
            db.flush()

            applied += 1
        except Exception as exc:
            errors.append(f"{side} {symbol.symbol} on {event_date}: {exc}")
            skipped += 1
            logger.warning("apply_backtest_run_to_portfolio event failed: %s", exc, exc_info=True)

    db.commit()

    # 统计剩余持仓数
    open_positions = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id)
    ).scalars().all()
    final_cash = cash_balance(db, portfolio_id)

    return {
        "run_id": run_id,
        "portfolio_id": portfolio_id,
        "initial_capital": initial_capital,
        "final_cash": _round_money(final_cash),
        "applied_trades": applied,
        "skipped_trades": skipped,
        "open_positions": len(open_positions),
        "errors": errors,
    }
