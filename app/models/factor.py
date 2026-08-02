from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Factor(Base):
    __tablename__ = "factors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_missing_policy: Mapped[str] = mapped_column(
        String(32), default="exclude"
    )
    is_active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    formula_expr: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- WP1-01: 生命周期与治理字段 ---
    # status 与 is_active 保持不变（向后兼容）；新增 lifecycle_status 用于治理工作流。
    origin: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # system/user/ai_assisted/imported
    lifecycle_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # draft/candidate/testing/shadow/active/quarantined/deprecated/rejected
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    factor_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # continuous/event/regime
    asset_scope_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: ["cn-stock", "cn-etf"]
    active_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("factor_versions.id", ondelete="SET NULL"), nullable=True
    )
    shadow_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("factor_versions.id", ondelete="SET NULL"), nullable=True
    )
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)  # low/medium/high
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class FactorValue(Base):
    __tablename__ = "factor_values"
    __table_args__ = (
        UniqueConstraint("symbol_id", "factor_id", "trade_date", "calc_batch_id", name="uq_factor_value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    factor_id: Mapped[int] = mapped_column(ForeignKey("factors.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    raw_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    normalized_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    calc_batch_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
