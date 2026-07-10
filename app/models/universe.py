from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UniverseSymbol(Base):
    """基础数据层：全市场标的元数据（与业务表 symbols 物理隔离）。

    挖掘任务只读此表，不写入 symbols 表。候选"晋升"时才从基础表复制到业务表。
    """

    __tablename__ = "universe_symbols"
    __table_args__ = (
        Index("ix_universe_symbol_asset", "asset_type", "region"),
        Index("ix_universe_symbol_synced", "is_synced", "last_bar_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(16))  # stock / etf
    market: Mapped[str] = mapped_column(String(16))  # sh / sz / bj
    region: Mapped[str] = mapped_column(String(8))  # cn / us
    board: Mapped[str | None] = mapped_column(String(32), nullable=True)  # main / star / gem
    industry: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # K线同步状态（冗余字段，加速健康度查询，避免 COUNT(*) 扫全表）
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_bar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bar_count: Mapped[int] = mapped_column(Integer, default=0)
    is_synced: Mapped[int] = mapped_column(Integer, default=0)  # 0=未同步 1=已同步
    sync_failed: Mapped[int] = mapped_column(Integer, default=0)  # 连续失败次数（熔断用）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )

    daily_bars = relationship("UniverseDailyBar", back_populates="universe_symbol_ref", cascade="all, delete-orphan")


class UniverseDailyBar(Base):
    """基础数据层：全市场 K 线数据（与业务表 daily_bars 物理隔离）。

    通过 universe_symbol_id 关联 universe_symbols，不与 symbols.id 产生外键关系。
    """

    __tablename__ = "universe_daily_bars"
    __table_args__ = (
        UniqueConstraint("universe_symbol_id", "trade_date", name="uq_universe_bar_symbol_date"),
        Index("ix_universe_bar_symbol_date", "universe_symbol_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    universe_symbol_id: Mapped[int] = mapped_column(
        ForeignKey("universe_symbols.id", ondelete="CASCADE"), index=True
    )
    trade_date: Mapped[date] = mapped_column(Date)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="akshare")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    universe_symbol_ref = relationship("UniverseSymbol", back_populates="daily_bars")
