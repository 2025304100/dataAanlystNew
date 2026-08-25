"""Immutable audit records for G5 new/old decision-chain comparisons."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class G5DualRunReport(Base):
    """One persisted G5 comparison result for a portfolio/date window.

    The full report remains JSON because it contains a variable-size per-day,
    per-symbol audit matrix. Gate fields are duplicated as typed columns for
    efficient, dialect-neutral readiness queries.
    """

    __tablename__ = "g5_dual_run_reports"
    __table_args__ = (
        Index("ix_g5_dual_run_reports_portfolio_created", "portfolio_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True,
    )
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    total_days: Mapped[int] = mapped_column(Integer)
    days_replayed: Mapped[int] = mapped_column(Integer)
    skipped_day_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    p0_unexplained_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    p1_hold_noaction_flip_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    eligible_for_g6: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True,
    )
