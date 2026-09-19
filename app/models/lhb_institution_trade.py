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


class LhbInstitutionTrade(Base):
    """Daily institution-seat totals from the Dragon-Tiger list."""

    __tablename__ = "lhb_institution_trades"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "trade_date",
            "source",
            name="uq_lhb_institution_trade_symbol_date_source",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    buyer_institution_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    seller_institution_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    institution_buy: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    institution_sell: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    institution_net: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    market_amount: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    institution_net_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    turnover_rate: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="akshare")
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
