"""Durable coverage points; calendar ranges must never imply interior coverage."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DataSyncCoverage(Base):
    """One observed dataset/symbol/day, separate from an execution's request range.

    ``symbol_id=0`` identifies a dataset-wide scan (for example an event list).
    It deliberately has no symbol foreign key. ``completed`` is an explicit
    provider/finality assertion, never inferred from an empty response or from
    the earliest and latest dates. Execution timestamps are naive UTC, matching
    DataSyncPlan; ``trade_date`` is the actual source date, not execution time.
    """

    __tablename__ = "data_sync_coverage"
    __table_args__ = (
        UniqueConstraint("dataset", "symbol_id", "trade_date", name="uq_data_sync_coverage_point"),
        Index("ix_data_sync_coverage_lookup", "dataset", "symbol_id", "completed", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(32), index=True)
    symbol_id: Mapped[int] = mapped_column(Integer, default=0)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    fields_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    first_synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, index=True)
    last_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
