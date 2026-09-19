"""市场指数日线模型（P3+：Benchmark 对比曲线基础设施）。

存储市场指数（如沪深300、上证指数）的日线行情，用于：
- 组合绩效面板的 benchmark 对比曲线
- 计算 alpha/beta/相对强弱等相对收益指标

设计要点：
- 不走 symbols 表：指数不是个股，用字符串 symbol 字段标识（如 "000300"）
- UniqueConstraint(symbol, trade_date) 防重
- trade_date 用 UTC 日期，与 DailyBar/PortfolioEquitySnapshot 对齐
- 历史可回溯：与组合 snapshot 不同，指数日线可一次性拉过去 N 年
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IndexPrice(Base):
    """市场指数日线。

    常用 symbol：
    - "000300"：沪深300
    - "000001"：上证指数
    - "399001"：深证成指
    - "399006"：创业板指
    """
    __tablename__ = "index_prices"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_index_price_symbol_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)

    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)

    source: Mapped[str] = mapped_column(String(32), default="akshare")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
