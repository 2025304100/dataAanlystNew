from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


IMPACT_SCOPE_CHOICES = [
    "macro_policy",
    "commodity_futures",
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
    impact_scope: Mapped[str] = mapped_column(String(32), default="other", index=True)
    importance_level: Mapped[int] = mapped_column(Integer, default=1, index=True)
    affected_market: Mapped[str] = mapped_column(String(64), default="A\u80a1/\u671f\u8d27", index=True)
    affected_sectors: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_symbols: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str] = mapped_column(String(16), default="neutral", index=True)
    source: Mapped[str] = mapped_column(String(64), default="manual", index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_manual: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
