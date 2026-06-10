from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    account_type: Mapped[str] = mapped_column(String(16))
    total_capital: Mapped[float] = mapped_column(Float)
    investable_ratio: Mapped[float] = mapped_column(Float)
    cash_reserve_ratio: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(16), default="CNY")
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    rules = relationship("PortfolioRule", back_populates="portfolio_ref", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="portfolio_ref", cascade="all, delete-orphan")


class PortfolioRule(Base):
    __tablename__ = "portfolio_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    rule_name: Mapped[str] = mapped_column(String(128))
    max_single_position_pct: Mapped[float] = mapped_column(Float)
    max_sector_position_pct: Mapped[float] = mapped_column(Float)
    max_stock_position_pct: Mapped[float] = mapped_column(Float)
    max_etf_position_pct: Mapped[float] = mapped_column(Float)
    max_loss_per_trade_pct: Mapped[float] = mapped_column(Float)
    max_open_positions: Mapped[int] = mapped_column(Integer)
    stage_limits_json: Mapped[str] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    portfolio_ref = relationship("Portfolio", back_populates="rules")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("portfolio_id", "symbol_id", name="uq_portfolio_symbol_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0)
    latest_price: Mapped[float] = mapped_column(Float, default=0)
    market_value: Mapped[float] = mapped_column(Float, default=0)
    position_pct: Mapped[float] = mapped_column(Float, default=0)
    asset_type: Mapped[str] = mapped_column(String(16))
    theme: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    portfolio_ref = relationship("Portfolio", back_populates="positions")

