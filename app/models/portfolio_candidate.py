"""Portfolio-scoped candidate pool.

Candidates are watch targets for one portfolio.  They intentionally do not
create a PortfolioMember or a position; promotion to a member is a separate
user action.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PortfolioCandidate(Base):
    __tablename__ = "portfolio_candidates"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "symbol_id", "effective_from", name="uq_portfolio_candidate_symbol_effective"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="RESTRICT"), index=True)
    effective_from: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
        default=date.today,
        comment="SCD2生效交易日（含当日）",
    )
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True, index=True, comment="SCD2失效交易日（含当日，NULL=至今有效）；同日多次修改不关闭旧区间，合并为当日最终态")
    auto_authorized_flag: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="是否自动授权为买入白名单（1=是0=否）")
    removed_manually_flag: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="是否手动移除出候选（1=是0=否）")
    removal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="移除原因：MANUAL_REMOVE/PROMOTED_TO_MEMBER/DELISTED/OTHER")
    audit_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="同一交易日变更序号1/2/3…同日多次修改递增")
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="SCD2 行最后变更时间（同日合并也更新此值）",
    )
