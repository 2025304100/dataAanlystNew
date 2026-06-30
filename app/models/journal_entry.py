from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    trade_setup_id: Mapped[int | None] = mapped_column(ForeignKey("trade_setups.id", ondelete="SET NULL"), nullable=True)
    entry_type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(128))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    subjective_view: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_system: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_id: Mapped[int | None] = mapped_column(ForeignKey("scores.id", ondelete="SET NULL"), nullable=True, index=True)
    stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actual_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

