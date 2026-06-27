from __future__ import annotations

import json
from datetime import date, datetime, timezone
from math import floor

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol


def _compute_cost(price: float, quantity: float, side: str, config: dict) -> float:
    """计算交易成本（佣金 + 印花税 + 滑点）"""
    commission_rate = float(config.get("commission_rate", 0.0003))
    min_commission = float(config.get("min_commission", 5.0))
    stamp_tax_rate = float(config.get("stamp_tax_rate", 0.001))
    slippage_rate = float(config.get("slippage_rate", 0.001))
    
    amount = price * quantity
    commission = max(amount * commission_rate, min_commission)
    slippage = amount * slippage_rate
    
    if side == "sell":
        stamp_tax = amount * stamp_tax_rate
        return commission + stamp_tax + slippage
    return commission + slippage


def _evaluate_buy_signal(
    symbol_id: int,
    trade_date: date,
    bar: DailyBar,
    score: Score | None,
    rule_config: dict,
) -> bool:
    """评估买入信号"""
    buy_conditions = rule_config.get("buy_conditions", {})
    
    # 质量分和时点分条件
    if score is None:
        return False
    
    quality_min = buy_conditions.get("quality_score_min")
    if quality_min is not None and score.quality_score < quality_min:
        return False
    
    timing_min = buy_conditions.get("timing_score_min")
    if timing_min is not None and score.timing_score < timing_min:
        return False
    
    # 阶段和动作条件
    allowed_stages = buy_conditions.get("stages")
    if allowed_stages and score.stage not in allowed_stages:
        return False
    
    allowed_actions = buy_conditions.get("actions")
    if allowed_actions and score.action not in allowed_actions:
        return False
    
    return True


def _evaluate_sell_signal(
    trade: BacktestTrade,
    current_bar: DailyBar,
    current_date: date,
    entry_date: date,
    rule_config: dict,
) -> tuple[bool, str | None]:
    """评估卖出信号，返回 (是否卖出, 卖出原因)"""
    sell_conditions = rule_config.get("sell_conditions", {})
    
    entry_price = trade.entry_price
    current_price = current_bar.close
    hold_days = (current_date - entry_date).days
    
    # 止盈
    take_profit_pct = sell_conditions.get("take_profit_pct")
    if take_profit_pct is not None:
        profit_pct = (current_price - entry_price) / entry_price
        if profit_pct >= take_profit_pct:
            return True, "take_profit"
    
    # 止损
    stop_loss_pct = sell_conditions.get("stop_loss_pct")
    if stop_loss_pct is not None:
        loss_pct = (entry_price - current_price) / entry_price
        if loss_pct >= stop_loss_pct:
            return True, "stop_loss"
    
    # 最大持有天数
    max_hold_days = sell_conditions.get("max_hold_days")
    if max_hold_days is not None and hold_days >= max_hold_days:
        return True, "max_hold_days"
    
    return False, None


def _compute_equity_curve(
    trades: list[BacktestTrade],
    initial_capital: float,
    date_range: list[date],
    close_prices: dict[tuple[int, date], float],
) -> list[dict]:
    """计算每日权益曲线"""
    equity_curve = []
    cash = initial_capital
    positions: dict[int, BacktestTrade] = {}
    
    for current_date in date_range:
        # 处理当日开仓
        for trade in trades:
            if trade.entry_date == current_date:
                cash -= trade.entry_price * trade.quantity + trade.entry_cost
                positions[trade.id] = trade
        
        # 处理当日平仓
        for trade in trades:
            if trade.exit_date == current_date and trade.id in positions:
                cash += trade.exit_price * trade.quantity - (trade.exit_cost or 0)
                del positions[trade.id]
        
        position_value = 0.0
        for trade in positions.values():
            close_price = close_prices.get((trade.symbol_id, current_date), trade.entry_price)
            position_value += close_price * trade.quantity

        equity = cash + position_value
        equity_curve.append({
            "date": current_date.isoformat(),
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "position_value": round(position_value, 2),
        })
    
    return equity_curve


def _compute_statistics(
    trades: list[BacktestTrade],
    initial_capital: float,
    equity_curve: list[dict],
) -> dict:
    """计算回测统计指标"""
    if not trades or not equity_curve:
        return {
            "total_return": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "trade_count": 0,
            "avg_holding_days": 0.0,
        }
    
    # 总收益
    final_equity = equity_curve[-1]["equity"]
    total_return = final_equity - initial_capital
    total_return_pct = total_return / initial_capital if initial_capital > 0 else 0.0
    
    # 最大回撤
    peak = initial_capital
    max_dd = 0.0
    max_dd_pct = 0.0
    for point in equity_curve:
        equity = point["equity"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        dd_pct = dd / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct
    
    # 胜率和盈亏比
    completed_trades = [t for t in trades if t.exit_date is not None and t.pnl is not None]
    win_trades = [t for t in completed_trades if t.pnl > 0]
    loss_trades = [t for t in completed_trades if t.pnl < 0]
    
    win_rate = len(win_trades) / len(completed_trades) if completed_trades else 0.0
    
    avg_win = sum(t.pnl for t in win_trades) / len(win_trades) if win_trades else 0.0
    avg_loss = abs(sum(t.pnl for t in loss_trades) / len(loss_trades)) if loss_trades else 0.0
    profit_factor = (avg_win * len(win_trades)) / (avg_loss * len(loss_trades)) if loss_trades else float('inf')
    
    # 平均持有天数
    avg_hold_days = sum(t.hold_days or 0 for t in completed_trades) / len(completed_trades) if completed_trades else 0.0
    
    # 夏普比率（简化版，假设无风险利率 3%）
    if len(equity_curve) > 1:
        returns = [(equity_curve[i]["equity"] - equity_curve[i-1]["equity"]) / equity_curve[i-1]["equity"]
                   for i in range(1, len(equity_curve))]
        mean_return = sum(returns) / len(returns) if returns else 0.0
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns) if returns else 0.0
        std = variance ** 0.5
        sharpe_ratio = (mean_return * 252 - 0.03) / (std * (252 ** 0.5)) if std > 0 else 0.0
    else:
        sharpe_ratio = 0.0
    
    return {
        "total_return": round(total_return, 2),
        "total_return_pct": round(total_return_pct, 4),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 999.0,
        "trade_count": len(trades),
        "avg_holding_days": round(avg_hold_days, 1),
    }


def run_backtest(
    db: Session,
    portfolio_id: int,
    symbol_ids: list[int],
    start_date: date,
    end_date: date,
    rule_config: dict,
    cost_config: dict | None = None,
    run_name: str | None = None,
) -> BacktestRun:
    """
    运行回测
    
    rule_config 结构：
    {
        "buy_conditions": {
            "quality_score_min": 60,
            "timing_score_min": 55,
            "stages": ["start", "accel"],
            "actions": ["open", "hold", "buy_dip"]
        },
        "sell_conditions": {
            "take_profit_pct": 0.15,
            "stop_loss_pct": 0.08,
            "max_hold_days": 30
        },
        "position_config": {
            "type": "fixed_pct",
            "value": 0.05,
            "max_positions": 10
        }
    }
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")
    
    cost_config = cost_config or {
        "commission_rate": 0.0003,
        "min_commission": 5.0,
        "stamp_tax_rate": 0.001,
        "slippage_rate": 0.001,
    }
    
    initial_capital = float(portfolio.total_capital)
    
    # 创建回测记录
    run = BacktestRun(
        portfolio_id=portfolio_id,
        run_name=run_name or f"Backtest {datetime.now().strftime('%Y%m%d_%H%M%S')}",
        symbols_json=json.dumps(symbol_ids),
        rule_config_json=json.dumps(rule_config, ensure_ascii=False),
        cost_config_json=json.dumps(cost_config, ensure_ascii=False),
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    
    try:
        # 获取交易日列表
        date_bars = db.execute(
            select(DailyBar.trade_date)
            .where(
                DailyBar.symbol_id.in_(symbol_ids),
                DailyBar.trade_date >= start_date,
                DailyBar.trade_date <= end_date,
            )
            .distinct()
            .order_by(DailyBar.trade_date)
        ).scalars().all()
        
        if not date_bars:
            raise ValueError("No trading data found in date range")

        close_prices = {
            (row.symbol_id, row.trade_date): float(row.close)
            for row in db.execute(
                select(DailyBar.symbol_id, DailyBar.trade_date, DailyBar.close).where(
                    DailyBar.symbol_id.in_(symbol_ids),
                    DailyBar.trade_date >= start_date,
                    DailyBar.trade_date <= end_date,
                )
            ).all()
        }
        
        # 仓位配置
        position_config = rule_config.get("position_config", {})
        position_type = position_config.get("type", "fixed_pct")
        position_value = float(position_config.get("value", 0.05))
        max_positions = int(position_config.get("max_positions", 10))
        
        cash = initial_capital
        open_trades: dict[int, BacktestTrade] = {}
        completed_trades: list[BacktestTrade] = []
        
        # 逐日模拟
        for current_date in date_bars:
            # 评估卖出信号
            for symbol_id, trade in list(open_trades.items()):
                bar = db.execute(
                    select(DailyBar)
                    .where(
                        DailyBar.symbol_id == symbol_id,
                        DailyBar.trade_date == current_date,
                    )
                ).scalars().first()
                
                if bar is None:
                    continue
                
                should_sell, exit_reason = _evaluate_sell_signal(
                    trade, bar, current_date, trade.entry_date, rule_config
                )
                
                if should_sell:
                    exit_price = bar.close
                    exit_cost = _compute_cost(exit_price, trade.quantity, "sell", cost_config)
                    pnl = (exit_price - trade.entry_price) * trade.quantity - trade.entry_cost - exit_cost
                    pnl_pct = pnl / (trade.entry_price * trade.quantity)
                    hold_days = (current_date - trade.entry_date).days
                    
                    trade.exit_date = current_date
                    trade.exit_price = exit_price
                    trade.exit_reason = exit_reason
                    trade.exit_cost = exit_cost
                    trade.pnl = round(pnl, 2)
                    trade.pnl_pct = round(pnl_pct, 4)
                    trade.hold_days = hold_days
                    
                    cash += exit_price * trade.quantity - exit_cost
                    completed_trades.append(trade)
                    del open_trades[symbol_id]
            
            # 评估买入信号
            if len(open_trades) < max_positions:
                for symbol_id in symbol_ids:
                    if symbol_id in open_trades:
                        continue
                    
                    bar = db.execute(
                        select(DailyBar)
                        .where(
                            DailyBar.symbol_id == symbol_id,
                            DailyBar.trade_date == current_date,
                        )
                    ).scalars().first()
                    
                    if bar is None:
                        continue
                    
                    # 获取评分
                    score = db.execute(
                        select(Score)
                        .where(
                            Score.symbol_id == symbol_id,
                            Score.trade_date <= current_date,
                        )
                        .order_by(Score.trade_date.desc())
                    ).scalars().first()
                    
                    if _evaluate_buy_signal(symbol_id, current_date, bar, score, rule_config):
                        # 计算买入数量
                        entry_price = bar.close
                        if position_type == "fixed_pct":
                            position_amount = initial_capital * position_value
                        else:
                            position_amount = position_value
                        
                        # 获取标的信息确定手数
                        symbol = db.get(Symbol, symbol_id)
                        lot_size = 100 if symbol and symbol.market in {"SH", "SZ", "BJ"} else 1
                        
                        quantity = floor(position_amount / entry_price / lot_size) * lot_size
                        if quantity <= 0:
                            continue
                        
                        entry_cost = _compute_cost(entry_price, quantity, "buy", cost_config)
                        total_cost = entry_price * quantity + entry_cost
                        
                        if total_cost > cash:
                            continue
                        
                        trade = BacktestTrade(
                            run_id=run.id,
                            symbol_id=symbol_id,
                            entry_date=current_date,
                            entry_price=entry_price,
                            quantity=quantity,
                            entry_cost=entry_cost,
                        )
                        db.add(trade)
                        db.flush()
                        
                        cash -= total_cost
                        open_trades[symbol_id] = trade
                        
                        if len(open_trades) >= max_positions:
                            break
        
        # 收集所有交易
        all_trades = completed_trades + list(open_trades.values())
        
        # 计算权益曲线和统计指标
        equity_curve = _compute_equity_curve(all_trades, initial_capital, date_bars, close_prices)
        stats = _compute_statistics(all_trades, initial_capital, equity_curve)
        
        # 更新回测记录
        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        run.total_return = stats["total_return"]
        run.total_return_pct = stats["total_return_pct"]
        run.max_drawdown = stats["max_drawdown"]
        run.max_drawdown_pct = stats["max_drawdown_pct"]
        run.sharpe_ratio = stats["sharpe_ratio"]
        run.win_rate = stats["win_rate"]
        run.profit_factor = stats["profit_factor"]
        run.trade_count = stats["trade_count"]
        run.avg_holding_days = stats["avg_holding_days"]
        run.equity_curve_json = json.dumps(equity_curve, ensure_ascii=False)
        
        db.commit()
        return run
    
    except Exception as e:
        run.status = "failed"
        run.error_message = str(e)
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise
