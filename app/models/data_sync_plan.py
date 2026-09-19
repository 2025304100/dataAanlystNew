from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DataSyncPlan(Base):
    """Frozen provider request and its durable partition progress."""

    __tablename__ = "data_sync_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sync_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dataset: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    requested_start_date: Mapped[date] = mapped_column(Date)
    requested_end_date: Mapped[date] = mapped_column(Date)
    partition_strategy: Mapped[str] = mapped_column(String(32))
    total_partitions: Mapped[int] = mapped_column(Integer, default=0)
    completed_partitions: Mapped[int] = mapped_column(Integer, default=0)
    skipped_partitions: Mapped[int] = mapped_column(Integer, default=0)
    failed_partitions: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )


class DataSyncPartition(Base):
    """Retryable symbol/date partition belonging to a frozen sync plan."""

    __tablename__ = "data_sync_partitions"
    __table_args__ = (
        UniqueConstraint("plan_id", "partition_key", name="uq_data_sync_partition_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("data_sync_plans.id", ondelete="CASCADE"), index=True
    )
    partition_key: Mapped[str] = mapped_column(String(160))
    symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )
