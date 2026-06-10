from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SignalRule(Base):
    __tablename__ = "signal_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    rule_name: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(32), default="balanced")
    quality_tolerance: Mapped[float] = mapped_column(Float, default=12.0)
    timing_tolerance: Mapped[float] = mapped_column(Float, default=12.0)
    min_sample_count: Mapped[int] = mapped_column(Integer, default=3)
    max_samples: Mapped[int] = mapped_column(Integer, default=60)
    same_region: Mapped[int] = mapped_column(Integer, default=1)
    same_asset_type: Mapped[int] = mapped_column(Integer, default=1)
    same_stage: Mapped[int] = mapped_column(Integer, default=1)
    same_action: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
