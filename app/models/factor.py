from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Factor(Base):
    __tablename__ = "factors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    formula_expr: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class FactorValue(Base):
    __tablename__ = "factor_values"
    __table_args__ = (
        UniqueConstraint("symbol_id", "factor_id", "trade_date", "calc_batch_id", name="uq_factor_value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    factor_id: Mapped[int] = mapped_column(ForeignKey("factors.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    raw_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    normalized_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    calc_batch_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

