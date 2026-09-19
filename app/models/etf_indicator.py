from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EtfIndicator(Base):
    """ETF 特有指标（溢价折价/跟踪误差/基金规模/份额变化）。

    P2：外部数据因子接入，用于评分引擎的 premium_discount 维度。
    数据源：akshare fund_etf_spot_em / fund_etf_fund_info_em / fund_etf_fund_daily_em。
    同一 symbol_id + trade_date 唯一。
    """

    __tablename__ = "etf_indicators"
    __table_args__ = (
        UniqueConstraint("symbol_id", "trade_date", name="uq_etf_indicator_symbol_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    # 净值与价格
    nav: Mapped[float | None] = mapped_column(Float, nullable=True)  # 基金净值（IOPV/单位净值）
    close: Mapped[float | None] = mapped_column(Float, nullable=True)  # 二级市场收盘价
    # 溢价折价率（%，正为溢价，负为折价）
    premium_discount: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 基金规模与份额
    fund_size: Mapped[float | None] = mapped_column(Float, nullable=True)  # 基金规模（元）
    total_shares: Mapped[float | None] = mapped_column(Float, nullable=True)  # 总份额（份）
    shares_change: Mapped[float | None] = mapped_column(Float, nullable=True)  # 份额变化（份，正为净申购）
    # 跟踪误差（%，应用层计算：ETF 收益率 vs 标的指数收益率 的滚动标准差）
    tracking_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 标准化评分（0-100，越高越优；由 calc_premium_discount_score 写入）
    premium_discount_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 元信息
    source: Mapped[str] = mapped_column(String(32), default="akshare")
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
