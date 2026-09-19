from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CapitalFlow(Base):
    """个股资金流数据（主力/超大单/大单/中单/小单 净流入）。

    P2：外部数据因子接入，用于评分引擎的 capital_flow 维度。
    数据源：akshare stock_individual_fund_flow。
    同一 symbol_id + trade_date 唯一。
    """

    __tablename__ = "capital_flows"
    __table_args__ = (
        UniqueConstraint("symbol_id", "trade_date", name="uq_capital_flow_symbol_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    # 净流入金额（元，正为净流入，负为净流出）
    main_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 主力净流入
    super_large_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 超大单净流入
    large_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 大单净流入
    medium_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 中单净流入
    small_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 小单净流入
    # 主力净流入占比（%）
    main_net_inflow_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 标准化评分（0-100，越高越优；由 calc_main_net_inflow_score 写入）
    main_net_inflow_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 元信息
    source: Mapped[str] = mapped_column(String(32), default="akshare")
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class NorthboundFlow(Base):
    """北向资金每日净流入（市场层面，非个股）。

    P2：外部数据因子接入，作为资金流维度的市场背景参考。
    数据源：akshare stock_hsgt_north_net_flow_in。
    同一 trade_date 唯一。
    """

    __tablename__ = "northbound_flows"
    __table_args__ = (
        UniqueConstraint("trade_date", name="uq_northbound_flow_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    sh_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 沪股通净流入（元）
    sz_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 深股通净流入（元）
    total_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 北向合计净流入
    # 元信息
    source: Mapped[str] = mapped_column(String(32), default="akshare")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
