"""绩效指标计算共享模块。

抽取自 backtest.py 的 _compute_statistics，
供回测、组合实盘绩效统计、（未来的）自动下单效果评估三方共用。

设计原则：
- 输入为通用结构（equity_curve: list[dict], trades: list[dict]），
  不耦合 BacktestTrade ORM 模型
- 纯函数，无 DB 依赖
- 算法与 backtest.py 原实现保持一致，确保回测结果可复现
"""
from __future__ import annotations

from math import sqrt
from typing import Any


# 年化交易日数（用于夏普比率等年化指标计算）
TRADING_DAYS_PER_YEAR = 252
# 无风险利率（用于夏普比率计算）
RISK_FREE_RATE = 0.03


def compute_max_drawdown(
    equity_curve: list[dict[str, Any]],
    initial_capital: float,
) -> tuple[float, float]:
    """计算最大回撤（绝对值 + 百分比）。

    Args:
        equity_curve: 净值曲线，每项需有 "equity" 字段
        initial_capital: 初始资金

    Returns:
        (max_drawdown_absolute, max_drawdown_pct)
    """
    if not equity_curve:
        return (0.0, 0.0)

    peak = float(initial_capital)
    max_dd = 0.0
    max_dd_pct = 0.0
    for point in equity_curve:
        equity = float(point.get("equity", 0))
        if equity > peak:
            peak = equity
        dd = peak - equity
        dd_pct = dd / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct
    return (round(max_dd, 2), round(max_dd_pct, 4))


def compute_sharpe_ratio(equity_curve: list[dict[str, Any]]) -> float:
    """计算夏普比率（年化）。

    Args:
        equity_curve: 净值曲线，每项需有 "equity" 字段

    Returns:
        年化夏普比率；数据不足或方差为 0 时返回 0.0
    """
    if len(equity_curve) <= 1:
        return 0.0

    returns: list[float] = []
    for i in range(1, len(equity_curve)):
        prev_eq = equity_curve[i - 1].get("equity", 0)
        curr_eq = equity_curve[i].get("equity", 0)
        # 风控：prev_eq=0 → 除零；NaN/inf → 污染统计；prev_eq<0 → 收益率符号反转
        if not isinstance(prev_eq, (int, float)) or prev_eq <= 0 or prev_eq != prev_eq:
            returns.append(0.0)
            continue
        if not isinstance(curr_eq, (int, float)) or curr_eq != curr_eq:
            returns.append(0.0)
            continue
        returns.append((curr_eq - prev_eq) / prev_eq)

    if not returns:
        return 0.0

    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
    std = variance ** 0.5
    if std <= 0:
        return 0.0
    sharpe = (mean_return * TRADING_DAYS_PER_YEAR - RISK_FREE_RATE) / (
        std * (TRADING_DAYS_PER_YEAR ** 0.5)
    )
    return round(sharpe, 2)


def compute_trade_statistics(trades: list[dict[str, Any]]) -> dict[str, float]:
    """计算交易统计：胜率、盈亏比、平均持仓天数。

    Args:
        trades: 交易列表，每项需有字段：
            - pnl: 已实现盈亏（可空，表示未平仓）
            - hold_days: 持仓天数（可空）

    Returns:
        dict: {
            "win_rate": 胜率,
            "profit_factor": 盈亏比（无亏损时返回 999.0）,
            "trade_count": 交易总数,
            "avg_holding_days": 平均持仓天数,
        }
    """
    completed = [
        t for t in trades
        if t.get("pnl") is not None
    ]
    if not completed:
        return {
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "trade_count": len(trades),
            "avg_holding_days": 0.0,
        }

    win_trades = [t for t in completed if float(t["pnl"]) > 0]
    loss_trades = [t for t in completed if float(t["pnl"]) < 0]

    win_rate = len(win_trades) / len(completed) if completed else 0.0

    avg_win = sum(float(t["pnl"]) for t in win_trades) / len(win_trades) if win_trades else 0.0
    avg_loss = abs(sum(float(t["pnl"]) for t in loss_trades) / len(loss_trades)) if loss_trades else 0.0
    gross_loss = avg_loss * len(loss_trades)
    profit_factor = (avg_win * len(win_trades)) / gross_loss if gross_loss > 0 else float("inf")

    avg_hold_days = sum(float(t.get("hold_days") or 0) for t in completed) / len(completed) if completed else 0.0

    return {
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
        "trade_count": len(trades),
        "avg_holding_days": round(avg_hold_days, 1),
    }


def compute_statistics(
    trades: list[dict[str, Any]],
    initial_capital: float,
    equity_curve: list[dict[str, Any]],
) -> dict[str, float]:
    """计算完整绩效统计（总收益 + 最大回撤 + 夏普 + 胜率 + 盈亏比）。

    抽取自 backtest.py._compute_statistics，保持算法一致。

    Args:
        trades: 交易列表（见 compute_trade_statistics）
        initial_capital: 初始资金
        equity_curve: 净值曲线

    Returns:
        完整统计 dict，字段与原 backtest._compute_statistics 一致：
        total_return, total_return_pct, max_drawdown, max_drawdown_pct,
        sharpe_ratio, win_rate, profit_factor, trade_count, avg_holding_days
    """
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

    final_equity = float(equity_curve[-1].get("equity", 0))
    total_return = final_equity - float(initial_capital)
    total_return_pct = total_return / float(initial_capital) if float(initial_capital) > 0 else 0.0

    max_dd, max_dd_pct = compute_max_drawdown(equity_curve, float(initial_capital))
    sharpe = compute_sharpe_ratio(equity_curve)
    trade_stats = compute_trade_statistics(trades)

    return {
        "total_return": round(total_return, 2),
        "total_return_pct": round(total_return_pct, 4),
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "sharpe_ratio": sharpe,
        "win_rate": trade_stats["win_rate"],
        "profit_factor": trade_stats["profit_factor"],
        "trade_count": trade_stats["trade_count"],
        "avg_holding_days": trade_stats["avg_holding_days"],
    }
