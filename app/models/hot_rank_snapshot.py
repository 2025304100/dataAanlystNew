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


class StockHotRankSnapshot(Base):
    """Daily EastMoney top-100 popularity snapshot."""

    __tablename__ = "stock_hot_rank_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "trade_date",
            "source",
            name="uq_stock_hot_rank_symbol_date_source",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    hot_rank: Mapped[int] = mapped_column(Integer)
    hot_rank_total: Mapped[int] = mapped_column(Integer)
    hot_rank_pct: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(64), default="akshare")
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
