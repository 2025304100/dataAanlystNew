"""Append-only new-admission governance; historical trading stays untouched."""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class G5AdmissionControl(Base):
    __tablename__ = "g5_admission_controls"
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)
    __table_args__ = (CheckConstraint("row_version >= 0", name="ck_g5_control_version"),)


class G5AdmissionEvent(Base):
    __tablename__ = "g5_admission_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    portfolio_id: Mapped[int | None] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=True)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("g5_dual_run_reports.id", ondelete="RESTRICT"), nullable=True)
    original_report_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inventory_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, nullable=False)
    operator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_event_id: Mapped[int | None] = mapped_column(ForeignKey("g5_admission_events.id", ondelete="RESTRICT"), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    audit_event_id: Mapped[int] = mapped_column(ForeignKey("data_governance_audit_events.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    __table_args__ = (
        CheckConstraint("event_type IN ('FREEZE','RELEASE','INVALIDATE_REPORT','RESTORE_REPORT')", name="ck_g5_admission_event_type"),
        Index("ix_g5_admission_scope_id", "scope_key", "id"),
        Index("ix_g5_admission_report_id", "report_id", "id"),
    )


def _immutable(mapper, connection, target):
    raise ValueError("G5 admission events are immutable; append a reviewed superseding event")


event.listen(G5AdmissionEvent, "before_update", _immutable)
event.listen(G5AdmissionEvent, "before_delete", _immutable)
