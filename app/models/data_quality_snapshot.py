"""Persisted field-level data quality evidence for the data center."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DataQualitySnapshot(Base):
    """An immutable quality observation for one formula input field."""

    __tablename__ = "data_quality_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(32), index=True)
    field: Mapped[str] = mapped_column(String(64), index=True)
    readiness: Mapped[str] = mapped_column(String(16), index=True)
    evaluation_mode: Mapped[str] = mapped_column(String(16), default="continuous")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    nonnull_rows: Mapped[int] = mapped_column(Integer, default=0)
    distinct_symbols: Mapped[int] = mapped_column(Integer, default=0)
    distinct_dates: Mapped[int] = mapped_column(Integer, default=0)
    first_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    latest_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stage 2.6 structured assessment columns.  Nullable keeps old snapshots
    # readable; the JSON metrics field remains the compatibility fallback.
    quality_level: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    quality_assessment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    missing_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_consecutive_gap_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quarantine_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, index=True
    )
