"""Portfolio-scoped candidate pool.

Candidates are watch targets for one portfolio.  They intentionally do not
create a PortfolioMember or a position; promotion to a member is a separate
user action.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PortfolioCandidate(Base):
    __tablename__ = "portfolio_candidates"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "symbol_id", name="uq_portfolio_candidate_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    source_candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 入池来源和当时证据是候选池的审计边界：不能只留下一个展示标签。
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_scan_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pool_memberships_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    admission_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommended_position_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    factor_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
