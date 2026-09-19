from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TailAccumulationSnapshot(Base):
    """Candidate-only minute-price/volume proxy; not Level-2 order flow."""

    __tablename__ = "tail_accumulation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "trade_date",
            "source",
            name="uq_tail_accumulation_symbol_date_source",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    minute_count: Mapped[int] = mapped_column(Integer)
    tail_minute_count: Mapped[int] = mapped_column(Integer)
    day_amount: Mapped[float] = mapped_column(Float)
    tail_amount: Mapped[float] = mapped_column(Float)
    tail_amount_share: Mapped[float] = mapped_column(Float)
    tail_activity_ratio: Mapped[float] = mapped_column(Float)
    tail_return: Mapped[float] = mapped_column(Float)
    close_location: Mapped[float] = mapped_column(Float)
    proxy_score: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(64), default="akshare")
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
