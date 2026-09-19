"""Durable quarantine records for invalid external-data partitions.

The sync partition remains the operational state machine.  This table keeps
the evidence that explains why a partition was isolated, including the raw
response hash and (when small enough) the original payload.  It is deliberately
append-only from the application service: releasing a quarantine creates a
new audit event instead of rewriting the original reason.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DataQualityQuarantine(Base):
    """One immutable quarantine observation for a data partition."""

    __tablename__ = "data_quality_quarantines"
    __table_args__ = (
        Index("ix_dq_quarantine_dataset_status", "dataset", "status"),
        Index("ix_dq_quarantine_partition", "partition_id"),
        Index("ix_dq_quarantine_correlation", "correlation_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    partition_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sync_partitions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    assessment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    field: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="quarantined", index=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict[str, object | None]:
        return {
            "id": self.id,
            "partition_id": self.partition_id,
            "assessment_id": self.assessment_id,
            "dataset": self.dataset,
            "field": self.field,
            "symbol": self.symbol,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "raw_payload_hash": self.raw_payload_hash,
            "source_name": self.source_name,
            "source_version": self.source_version,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
        }


__all__ = ["DataQualityQuarantine"]
