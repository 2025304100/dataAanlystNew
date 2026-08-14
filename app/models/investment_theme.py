from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class InvestmentTheme(Base):
    """Governed investment theme, distinct from broad asset classifications."""

    __tablename__ = "investment_themes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default="manual")
    is_active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class SymbolThemeMapping(Base):
    """Traceable many-to-many mapping between symbols and investment themes."""

    __tablename__ = "symbol_theme_mappings"
    __table_args__ = (UniqueConstraint("theme_id", "symbol_id", name="uq_symbol_theme_mapping"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    theme_id: Mapped[int] = mapped_column(ForeignKey("investment_themes.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    is_confirmed: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ThemeCatalyst(Base):
    """Evidence that makes a theme timely instead of merely descriptive."""

    __tablename__ = "theme_catalysts"
    __table_args__ = (UniqueConstraint("theme_id", "market_event_id", name="uq_theme_market_event_catalyst"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    theme_id: Mapped[int] = mapped_column(ForeignKey("investment_themes.id", ondelete="CASCADE"), index=True)
    market_event_id: Mapped[int | None] = mapped_column(ForeignKey("market_events.id", ondelete="SET NULL"), nullable=True, index=True)
    title_snapshot: Mapped[str] = mapped_column(Text)
    catalyst_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source_type: Mapped[str] = mapped_column(String(32), default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
