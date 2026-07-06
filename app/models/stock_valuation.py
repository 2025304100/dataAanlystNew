from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StockValuation(Base):
    """股票估值数据（PE/PB/市值/行业分位）。

    P2：外部数据因子接入，用于评分引擎的 valuation 维度。
    数据源：akshare stock_zh_a_spot_em / stock_a_lg_indicator / stock_individual_info_em。
    同一 symbol_id + trade_date 唯一。
    """

    __tablename__ = "stock_valuations"
    __table_args__ = (
        UniqueConstraint("symbol_id", "trade_date", name="uq_stock_valuation_symbol_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    # 估值指标（可能为空，数据源不同时段覆盖度不同）
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)  # 总市值（元）
    circulating_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)  # 流通市值（元）
    # 行业分位（0-100，PE 在所属行业内的百分位，越低越被低估）
    industry_pe_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    industry: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 历史分位（0-100，当前 PE 在自身近 N 日历史中的百分位）
    pe_history_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 标准化评分（0-100，越高越优；由 calc_pe_score 写入）
    pe_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 元信息
    source: Mapped[str] = mapped_column(String(32), default="akshare")
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # 原始返回，便于追溯
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
