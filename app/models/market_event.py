from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

IMPACT_SCOPE_CHOICES = [
    "macro_policy",
    "sector_dynamics",
    "international",
    "breaking",
    "fund_flow",
    "sentiment",
    "other",
]


class MarketEvent(Base):
    __tablename__ = "market_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact_scope: Mapped[str] = mapped_column(String(32), default="other")
    importance_level: Mapped[int] = mapped_column(Integer, default=1)
    affected_market: Mapped[str] = mapped_column(String(64), default="A股")
    affected_sectors: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_symbols: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str] = mapped_column(String(16), default="neutral")
    source: Mapped[str] = mapped_column(String(64), default="manual")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_manual: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )