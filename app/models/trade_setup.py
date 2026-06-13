from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TradeSetup(Base):
    __tablename__ = "trade_setups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    score_id: Mapped[int] = mapped_column(ForeignKey("scores.id", ondelete="CASCADE"), index=True)
    scan_run_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True)
    stage: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(16))
    entry_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommended_position_pct: Mapped[float] = mapped_column(Float)
    recommended_position_amount: Mapped[float] = mapped_column(Float)
    risk_reward_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    allow_add_position: Mapped[int] = mapped_column(Integer, default=0)
    is_sector_overweight: Mapped[int] = mapped_column(Integer, default=0)
    is_asset_overweight: Mapped[int] = mapped_column(Integer, default=0)
    setup_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    trade_setup_id: Mapped[int] = mapped_column(ForeignKey("trade_setups.id", ondelete="CASCADE"), index=True)
    signal_type: Mapped[str] = mapped_column(String(16))
    signal_level: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

