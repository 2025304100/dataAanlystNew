"""组合实盘绩效计算服务（P1-1）。

基于 PortfolioEquitySnapshot 时序数据 + SimTrade 已实现交易，
调用 metrics 共享模块计算组合级绩效指标（最大回撤/夏普/胜率/盈亏比等）。

与 backtest 的区别：
- backtest 基于历史 K 线回放，equity_curve 是模拟的
- 本模块基于实盘 snapshot，equity_curve 是真实每日快照
- 共用 metrics 模块确保算法一致，便于"回测 vs 实盘"对比

数据来源：
- equity_curve: PortfolioEquitySnapshot.total_equity + snapshot_date
- trades: SimTrade 中 side='sell' 且 realized_pnl != None 的记录
- hold_days: 查找同 symbol 的最近一次 buy trade，计算 created_at 时间差

边界处理：
- 无 snapshot 或 snapshot < 2 条：绩效指标返回 0
- 无卖出 trade：win_rate/profit_factor/trade_count 为 0
- 单笔 trade hold_days 无法推算时为 0
"""
from __future__ import annotations

import bisect
import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio
from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
from app.models.sim_account import SimTrade
from app.services.index_data import list_index_prices
from app.services.metrics import (
    compute_max_drawdown,
    compute_sharpe_ratio,
    compute_statistics,
)
from app.services.portfolio_equity_snapshot import list_portfolio_equity_snapshots


logger = logging.getLogger(__name__)


def _snapshot_to_equity_curve(snapshots: list[PortfolioEquitySnapshot]) -> list[dict[str, Any]]:
    """把 snapshot 序列转换为 metrics 模块所需的 equity_curve 格式。

    格式：[{"date": "YYYY-MM-DD", "equity": float}, ...]
    顺序：按 snapshot_date 升序（与 list_portfolio_equity_snapshots 一致）
    """
    return [
        {
            "date": snap.snapshot_date.isoformat() if snap.snapshot_date else "",
            "equity": float(snap.total_equity or 0),
        }
        for snap in snapshots
    ]


def _find_recent_buy_date(db: Session, portfolio_id: int, symbol_id: int, before: datetime) -> datetime | None:
    """查找指定时间之前同 symbol 的最近一次买入 trade 时间（用于计算 hold_days）。

    Args:
        before: 卖出 trade 的 created_at
    Returns:
        买入 trade 的 created_at；找不到返回 None
    """
    return db.execute(
        select(SimTrade.created_at)
        .where(
            SimTrade.portfolio_id == portfolio_id,
            SimTrade.symbol_id == symbol_id,
            SimTrade.side == "buy",
            SimTrade.created_at <= before,
        )
        .order_by(desc(SimTrade.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _collect_completed_trades(
    db: Session, portfolio_id: int, *, start_date: date | None, end_date: date | None
) -> list[dict[str, Any]]:
    """收集已平仓交易（卖出 trade 且 realized_pnl 非 None），转为 metrics 格式。

    每项：{"pnl": float, "hold_days": int}
    hold_days 通过查找同 symbol 的最近买入 trade 推算；找不到为 0。
    """
    stmt = (
        select(SimTrade)
        .where(
            SimTrade.portfolio_id == portfolio_id,
            SimTrade.side == "sell",
            SimTrade.realized_pnl.is_not(None),
        )
        .order_by(SimTrade.created_at.asc())
    )
    # 按交易时间过滤（与 snapshot 时间范围对齐）
    if start_date is not None:
        # start_date 是日期，转 datetime 范围（含当日）
        stmt = stmt.where(SimTrade.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date is not None:
        end_dt = datetime.combine(end_date, datetime.max.time())
        stmt = stmt.where(SimTrade.created_at <= end_dt)

    sell_trades = list(db.execute(stmt).scalars().all())
    if not sell_trades:
        return []

    trades: list[dict[str, Any]] = []
    for sell in sell_trades:
        pnl = float(sell.realized_pnl or 0)
        hold_days = 0
        if sell.created_at is not None:
            # created_at 可能带 tz 也可能不带，统一转 naive UTC 比较
            sell_dt = sell.created_at
            if sell_dt.tzinfo is not None:
                sell_dt = sell_dt.replace(tzinfo=None)
            buy_dt = _find_recent_buy_date(db, portfolio_id, sell.symbol_id, sell_dt)
            if buy_dt is not None:
                if buy_dt.tzinfo is not None:
                    buy_dt = buy_dt.replace(tzinfo=None)
                hold_days = max(0, (sell_dt - buy_dt).days)
        trades.append({"pnl": pnl, "hold_days": hold_days})
    return trades


# 默认 benchmark：沪深300（A 股市场代表性强，与组合 snapshot 日期对齐度高）
_DEFAULT_BENCHMARK_SYMBOL = "000300"

# 常用 benchmark 名称映射（前端展示用）
_BENCHMARK_NAMES = {
    "000300": "沪深300",
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
}


def _build_benchmark_curve(
    db: Session,
    equity_curve: list[dict[str, Any]],
    *,
    benchmark_symbol: str,
    start_date: date | None,
    end_date: date | None,
    initial_capital: float,
) -> list[dict[str, Any]]:
    """构造 benchmark 净值曲线，与 equity_curve 日期对齐。

    策略：
    - 查询 IndexPrice 在 [start_date, end_date] 范围内的日线
    - 对每个 equity_curve 日期，取"当日或之前最近"的 benchmark close（前向填充）
    - 归一化：首条对齐的 close 缩放到 initial_capital，后续按比例
      → 两条曲线起点相同，偏差即超额/落后

    边界：
    - benchmark_symbol 为空 → 返回 []
    - 无 benchmark 数据 → 返回 []
    - 无日期重叠 → 返回 []
    """
    if not benchmark_symbol or not equity_curve:
        return []

    # 查询范围比 equity_curve 稍宽，确保首日能找到 benchmark close
    # 前向填充需要 eq_date 之前的数据（如周末/节假日 equity 有数据但 benchmark 无当日数据，
    # 需取前一个交易日的 close），所以 query_start 至少往前推 30 天
    query_start = start_date
    if query_start is None and equity_curve:
        query_start = date.fromisoformat(equity_curve[0]["date"])
    if query_start is not None:
        query_start = query_start - timedelta(days=30)
    query_end = end_date
    if query_end is None and equity_curve:
        query_end = date.fromisoformat(equity_curve[-1]["date"])

    bars = list_index_prices(
        db,
        benchmark_symbol,
        start_date=query_start,
        end_date=query_end,
        limit=5000,
    )
    if not bars:
        return []

    # 构建 date -> close 映射（升序）
    sorted_bars = sorted(bars, key=lambda b: b.trade_date)
    bar_dates = [b.trade_date for b in sorted_bars]
    bar_closes = [float(b.close) for b in sorted_bars]

    # 用 bisect 做"on or before"查找
    benchmark_curve: list[dict[str, Any]] = []
    base_close: float | None = None

    for point in equity_curve:
        eq_date_str = point.get("date", "")
        if not eq_date_str:
            continue
        try:
            eq_date = date.fromisoformat(eq_date_str)
        except ValueError:
            continue

        # bisect_right 找到第一个 > eq_date 的位置，-1 即 <= eq_date 的最后一条
        idx = bisect.bisect_right(bar_dates, eq_date) - 1
        if idx < 0:
            # 当日及之前均无 benchmark 数据，跳过（首点对齐失败）
            continue
        close = bar_closes[idx]
        if base_close is None:
            base_close = close
            if base_close <= 0:
                # 异常数据，放弃
                return []
        # 归一化到 initial_capital 起点
        scaled = initial_capital * (close / base_close) if base_close > 0 else 0.0
        benchmark_curve.append({
            "date": eq_date_str,
            "equity": round(scaled, 2),
        })

    return benchmark_curve


def compute_portfolio_performance(
    db: Session,
    portfolio_id: int,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    snapshot_limit: int = 1000,
    benchmark: str | None = _DEFAULT_BENCHMARK_SYMBOL,
) -> dict[str, Any]:
    """计算组合实盘绩效指标。

    Args:
        portfolio_id: 组合 ID（必须 account_type="simulated"）
        start_date: 起始日期（含），None 表示从最早 snapshot
        end_date: 结束日期（含），None 表示到最新 snapshot
        snapshot_limit: snapshot 查询上限（防止超大组合爆内存）
        benchmark: 基准指数代码（如 "000300" 沪深300）。
            None 或空串表示不返回 benchmark 曲线。
            默认 "000300"。

    Returns:
        {
            "portfolio_id": int,
            "initial_capital": float,
            "snapshot_count": int,
            "date_range": {"start": "YYYY-MM-DD" | None, "end": "YYYY-MM-DD" | None},
            "equity_curve": [{"date": str, "equity": float}, ...],
            "benchmark_curve": [{"date": str, "equity": float}, ...],  # 可能为空
            "benchmark_name": str | None,
            "stats": {
                "total_return": float,
                "total_return_pct": float,
                "max_drawdown": float,
                "max_drawdown_pct": float,
                "sharpe_ratio": float,
                "win_rate": float,
                "profit_factor": float,
                "trade_count": int,
                "avg_holding_days": float,
            }
        }

    边界处理：
    - 无 snapshot：所有指标为 0，equity_curve 为空
    - snapshot < 2 条：sharpe 为 0（无法计算收益率序列）
    - 无卖出 trade：win_rate/profit_factor/trade_count 为 0
    - 无 benchmark 数据：benchmark_curve 为空，benchmark_name 仍返回（前端降级为单 series）
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    snapshots = list_portfolio_equity_snapshots(
        db,
        portfolio_id,
        start_date=start_date,
        end_date=end_date,
        limit=snapshot_limit,
    )

    equity_curve = _snapshot_to_equity_curve(snapshots)
    initial_capital = float(portfolio.total_capital or 0)

    # 收集已平仓交易（与 snapshot 时间范围对齐）
    trades = _collect_completed_trades(
        db, portfolio_id, start_date=start_date, end_date=end_date
    )

    # 调用共享 metrics 模块计算统计
    # 注意：metrics.compute_statistics 在 trades 为空时返回全 0，
    # 但实盘组合可能"有 snapshot 无卖出 trade"（持仓未平），
    # 此时 total_return/max_drawdown/sharpe 仍应基于 equity_curve 计算。
    # 因此分步计算：trades 非空时用聚合函数，trades 为空时单独算 equity 指标。
    if trades:
        stats = compute_statistics(
            trades=trades,
            initial_capital=initial_capital,
            equity_curve=equity_curve,
        )
    elif equity_curve:
        # 无交易但有净值曲线：只算 equity 相关指标，trade 统计为 0
        final_equity = float(equity_curve[-1].get("equity", 0))
        total_return = final_equity - initial_capital
        total_return_pct = total_return / initial_capital if initial_capital > 0 else 0.0
        max_dd, max_dd_pct = compute_max_drawdown(equity_curve, initial_capital)
        sharpe = compute_sharpe_ratio(equity_curve)
        stats = {
            "total_return": round(total_return, 2),
            "total_return_pct": round(total_return_pct, 4),
            "max_drawdown": max_dd,
            "max_drawdown_pct": max_dd_pct,
            "sharpe_ratio": sharpe,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "trade_count": 0,
            "avg_holding_days": 0.0,
        }
    else:
        stats = compute_statistics(
            trades=trades,
            initial_capital=initial_capital,
            equity_curve=equity_curve,
        )

    # 构造 benchmark 对比曲线（归一化到 initial_capital 起点）
    benchmark_curve: list[dict[str, Any]] = []
    benchmark_name: str | None = None
    if benchmark:
        benchmark_curve = _build_benchmark_curve(
            db,
            equity_curve,
            benchmark_symbol=benchmark,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
        )
        benchmark_name = _BENCHMARK_NAMES.get(benchmark, benchmark)

    return {
        "portfolio_id": portfolio_id,
        "initial_capital": round(initial_capital, 2),
        "snapshot_count": len(snapshots),
        "date_range": {
            "start": snapshots[0].snapshot_date.isoformat() if snapshots else None,
            "end": snapshots[-1].snapshot_date.isoformat() if snapshots else None,
        },
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
        "benchmark_name": benchmark_name,
        "stats": stats,
    }


__all__ = ["compute_portfolio_performance"]
