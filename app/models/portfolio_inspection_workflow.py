"""Durable source receipts and append-only reviewed evidence associations."""
from datetime import date, datetime, timezone
from sqlalchemy import Date, DateTime, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReconciliationSourceEvent(Base):
    __tablename__ = "reconciliation_source_events"
    __table_args__ = (Index("ix_rse_pending_portfolio", "status", "portfolio_id", "id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    source_table: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation: Mapped[str] = mapped_column(String(16))
    facts_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processing_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReconciliationRecheckRequest(Base):
    """Submission receipt; never a reconciliation verdict or release authority."""
    __tablename__ = "reconciliation_recheck_requests"
    __table_args__ = (UniqueConstraint("portfolio_id", "request_key", name="uq_rrr_portfolio_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128))
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ReconciliationRequestAnnotation(Base):
    """Append-only provenance; never changes the retained request or verdict."""
    __tablename__ = "reconciliation_request_annotations"
    __table_args__ = (UniqueConstraint("request_id", "source_code", name="uq_rra_request_source"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    source_code: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text)
    evidence_ref: Mapped[str] = mapped_column(String(512))
    annotated_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class InspectionEvidenceRequest(Base):
    __tablename__ = "inspection_evidence_requests"
    __table_args__ = (UniqueConstraint("portfolio_id", "request_key", name="uq_ier_portfolio_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    baseline_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(128))
    source_hash: Mapped[str] = mapped_column(String(64))
    source_snapshot_json: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class InspectionEvidenceDecision(Base):
    __tablename__ = "inspection_evidence_decisions"
    __table_args__ = (UniqueConstraint("request_id", "decision_key", name="uq_ied_request_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    decision: Mapped[str] = mapped_column(String(16))
    decision_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    review_note: Mapped[str] = mapped_column(Text)
    reviewed_by: Mapped[str] = mapped_column(String(128))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


def _immutable(*_):
    raise ValueError("已保存的补证申请与审核不可改写，请保留原记录并新增申请")


for _model in (InspectionEvidenceRequest, InspectionEvidenceDecision, ReconciliationRequestAnnotation):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
