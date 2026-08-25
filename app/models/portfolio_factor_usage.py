"""Portfolio ↔ FactorSet ↔ FactorModelRun 绑定。

一组合只允许 **一条当前 APPLIED 绑定**（portfolio_id 唯一）。
保存时走 save_and_apply_usage_atomic() 单事务 7 步原子性 + row_version 乐观锁。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index,
    CheckConstraint, text as sqltxt,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

BINDING_STATUS_DRAFT = "DRAFT"
BINDING_STATUS_APPLIED = "APPLIED"
BINDING_STATUS_RETIRED = "RETIRED"
VALID_BINDING_STATUSES = frozenset({
    BINDING_STATUS_DRAFT, BINDING_STATUS_APPLIED, BINDING_STATUS_RETIRED,
})


class PortfolioFactorUsage(Base):
    """组合 ↔ 因子模型/FactorSet 绑定（单个组合仅保留一条 APPLIED 行）。"""

    __tablename__ = "portfolio_factor_usage"
    __table_args__ = (
        # portfolio_id 单列索引(UNIQUE)：测试通过 unique index 查找；同时也保留 UniqueConstraint
        Index("ux_pfu_portfolio_unique", "portfolio_id", unique=True),
        UniqueConstraint("portfolio_id", name="uq_portfolio_factor_usage_portfolio"),
        Index("ix_pfu_factor_model_run_id", "factor_model_run_id"),
        Index("ix_pfu_factor_set_id", "factor_set_id"),
        CheckConstraint(
            "binding_status IN ('DRAFT','APPLIED','RETIRED')",
            name="ck_pfu_binding_status_values",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True, nullable=False,
    )
    factor_model_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("factor_model_runs.id", ondelete="RESTRICT"), nullable=True,
        comment="研究模式可以无模型，正式 P0 必须非空",
    )
    factor_set_id: Mapped[int] = mapped_column(
        ForeignKey("factor_sets.id", ondelete="RESTRICT"), nullable=False,
    )
    factor_weights_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="T4a canonical hash：对 factor_weights + factor_set_id + model_run_id 做 SHA256",
    )
    binding_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BINDING_STATUS_DRAFT,
        server_default=BINDING_STATUS_DRAFT, index=True,
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sqltxt("0"),
        comment="乐观锁版本号：UPDATE ... WHERE id=? AND row_version=?，成功 row_version+1",
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
