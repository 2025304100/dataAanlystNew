"""Persistent account baselines and versioned reconciliation coverage.

Acceptance creates a new coverage generation; it does not certify any day or
change the legacy portfolio checkpoint, runtime permissions, or account book.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PortfolioAccountBaseline(Base):
    __tablename__ = "portfolio_account_baselines"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "acceptance_key", name="uq_pab_portfolio_acceptance"),
        CheckConstraint("kind IN ('initial','rebaseline')", name="ck_pab_kind"),
        Index("ix_pab_portfolio_scope_time", "portfolio_id", "scope_key", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False, default="account")
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    coverage_start_trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refs_json: Mapped[str] = mapped_column(Text, nullable=False)
    history_gaps_json: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_audit_id: Mapped[int] = mapped_column(ForeignKey("data_governance_audit_events.id", ondelete="RESTRICT"), nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("portfolio_account_baselines.id", ondelete="RESTRICT"), nullable=True)
    acceptance_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class PortfolioReconciliationCursor(Base):
    __tablename__ = "portfolio_reconciliation_cursors"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "scope_key", name="uq_prc_portfolio_scope"),
        CheckConstraint("generation >= 1 AND row_version >= 1", name="ck_prc_versions"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False, default="account")
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account_baselines.id", ondelete="RESTRICT"), nullable=False)
    coverage_start_trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    verified_through_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    calendar_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class PortfolioReconciliationDay(Base):
    __tablename__ = "portfolio_reconciliation_days"
    __table_args__ = (
        UniqueConstraint("cursor_id", "generation", "target_trade_date", name="uq_prd_cursor_generation_day"),
        CheckConstraint("work_state IN ('queued','running','waiting_execution','waiting_data','needs_review','done','cancelled')", name="ck_prd_work_state"),
        CheckConstraint("generation >= 1 AND requested_evidence_version >= 1 AND processed_evidence_version >= 0", name="ck_prd_versions"),
        Index("ix_prd_work_next_check", "work_state", "next_check_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_id: Mapped[str] = mapped_column(ForeignKey("portfolio_reconciliation_cursors.id", ondelete="RESTRICT"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    target_trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    work_state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    verdict: Mapped[str] = mapped_column(String(64), nullable=False, default="EVIDENCE_INCOMPLETE")
    closure_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_evidence_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    processed_evidence_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    account_source_pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Kept as a versioned pointer rather than a circular FK to the report.
    current_report_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class PortfolioReconciliationReport(Base):
    __tablename__ = "portfolio_reconciliation_reports"
    __table_args__ = (
        UniqueConstraint("day_id", "report_version", name="uq_prr_day_version"),
        CheckConstraint("report_version >= 1 AND evidence_version >= 1", name="ck_prr_versions"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day_id: Mapped[str] = mapped_column(ForeignKey("portfolio_reconciliation_days.id", ondelete="RESTRICT"), nullable=False)
    report_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_version: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account_baselines.id", ondelete="RESTRICT"), nullable=False)
    checks_json: Mapped[str] = mapped_column(Text, nullable=False)
    findings_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(64), nullable=False)
    closure_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    calendar_version: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_event_id: Mapped[int] = mapped_column(ForeignKey("data_governance_audit_events.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


def _immutable(mapper, connection, target) -> None:
    raise ValueError("Accepted baselines and versioned reports are immutable; create a new version")


for _model in (PortfolioAccountBaseline, PortfolioReconciliationReport):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
