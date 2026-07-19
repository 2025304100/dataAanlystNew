"""组合净值快照模型（P0-8）。

每日收盘后写入一条快照，记录组合的：
- 现金余额、市值、总权益
- 当日收益率
- 已实现/未实现盈亏

这是组合绩效统计（最大回撤、Sharpe、胜率等）的基础数据。
历史数据无法回溯，必须在系统上线后每日写入累积。

设计要点：
- 一组合一天一条，UniqueConstraint(portfolio_id, snapshot_date) 防重
- snapshot_date 用 UTC 日期，与 DailyBar.trade_date 对齐
- 不存储 benchmark 数据（基准独立存储，查询时 join）
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PortfolioEquitySnapshot(Base):
    """组合每日净值快照。

    用途：
    - 计算实盘组合的最大回撤、Sharpe、日收益率时序
    - 与回测结果对比，验证策略实盘表现
    - 多组合横向对比（归一化净值曲线）
    """
    __tablename__ = "portfolio_equity_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "snapshot_date", name="uq_portfolio_equity_snapshot_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)

    # 资产快照
    cash_balance: Mapped[float] = mapped_column(Float, default=0)
    market_value: Mapped[float] = mapped_column(Float, default=0)
    total_equity: Mapped[float] = mapped_column(Float, default=0)

    # 盈亏快照（累计）
    realized_pnl: Mapped[float] = mapped_column(Float, default=0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0)

    # 当日收益率（相对前一日总权益）
    # 第一天为 0；后续 = (今日 total_equity - 昨日 total_equity) / 昨日 total_equity
    daily_return: Mapped[float] = mapped_column(Float, default=0)

    # 持仓数量（冗余字段，便于绩效分析时不需要 join positions 表）
    position_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
