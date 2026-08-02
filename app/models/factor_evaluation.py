"""WP1-02: 因子治理与评估主表。

包含 EvaluationRun、TransitionAudit、FactorSet、FactorSetMember。
对齐 docs/专业因子库开发计划.md §WP1-02 和 docs/因子设置与专业因子库改造方案.md §7.3-7.4。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EvaluationRun(Base):
    """评估运行记录（不可变）。

    WP1 建立基础表结构，完整评估字段（metrics_json、gate_result 等）在 WP5 填充。
    """

    __tablename__ = "factor_evaluation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    factor_version_id: Mapped[int] = mapped_column(
        ForeignKey("factor_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    universe_snapshot_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # 冻结股票池标识
    data_cutoff_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    target_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default="target_5d_return"
    )
    train_start_date: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Use DateTime for consistency
    train_end_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    validation_start_date: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    validation_end_date: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    config_json: Mapped[str] = mapped_column(Text, default="{}")  # 分组/成本/扰动/门禁配置
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")  # IC/ICIR/单调性/收益/换手
    gate_result: Mapped[str | None] = mapped_column(
        String(24), nullable=True, index=True
    )  # passed/rejected/warn
    rejection_reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    task_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )  # 对应异步任务
    created_by: Mapped[str] = mapped_column(String(128), default="local_user")
    # WPD-02 完整交易日证据
    selected_trade_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    observed_symbols: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_symbols: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completeness_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, nullable=False
    )

    __table_args__ = (
        Index("ix_evaluation_runs_factor_cutoff", "factor_version_id", "data_cutoff_at"),
    )


class TransitionAudit(Base):
    """因子状态迁移审计记录（追加式，不可更新或删除）。"""

    __tablename__ = "factor_transition_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_id: Mapped[int] = mapped_column(
        ForeignKey("factors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    factor_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("factor_versions.id", ondelete="SET NULL"), nullable=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), default="local_user")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("factor_evaluation_runs.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # 幂等键
    migration_note: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # 迁移说明（system_migration 等）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_transition_audits_factor_created", "factor_id", "created_at"),
    )


class FactorSet(Base):
    """因子集合（发布后不可修改成员）。"""

    __tablename__ = "factor_sets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )  # 成员版本聚合哈希
    status: Mapped[str] = mapped_column(
        String(32), default="draft", nullable=False, index=True
    )  # draft/frozen/deprecated
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="local_user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive, nullable=False
    )

    members: Mapped[list["FactorSetMember"]] = relationship(
        "FactorSetMember",
        back_populates="factor_set",
        cascade="all, delete-orphan",
    )


class FactorSetMember(Base):
    """因子集合成员（固定因子版本）。"""

    __tablename__ = "factor_set_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_set_id: Mapped[str] = mapped_column(
        ForeignKey("factor_sets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    factor_id: Mapped[int] = mapped_column(
        ForeignKey("factors.id", ondelete="CASCADE"), nullable=False
    )
    factor_version_id: Mapped[int] = mapped_column(
        ForeignKey("factor_versions.id", ondelete="CASCADE"), nullable=False
    )
    factor_code: Mapped[str] = mapped_column(String(64), nullable=False)
    factor_version: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="feature")  # feature/target/regime
    weight_constraint: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # positive/negative/free
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    missing_policy: Mapped[str] = mapped_column(
        String(32), default="exclude"
    )  # exclude/impute_zero/ignore
    excluded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, nullable=False
    )

    factor_set: Mapped["FactorSet"] = relationship(
        "FactorSet", back_populates="members"
    )

    __table_args__ = (
        UniqueConstraint("factor_set_id", "factor_id", name="uq_factor_set_member"),
    )


class ShadowObservation(Base):
    """Shadow 因子每日观测记录（WP6-03）。

    按因子版本和交易日幂等记录，用于跟踪 Shadow 因子在真实日常数据中的稳定性。
    观察期规则：
    - 至少连续 20 个有效交易日才允许提交 Active
    - 缺少交易日/横截面完整率<90%/数据异常不计入有效观察天数
    - 不允许用历史回测结果回填 Shadow 天数
    """

    __tablename__ = "factor_shadow_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_id: Mapped[int] = mapped_column(
        ForeignKey("factors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    factor_version_id: Mapped[int] = mapped_column(
        ForeignKey("factor_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trade_date: Mapped[str] = mapped_column(
        String(10), nullable=False, index=True
    )  # YYYY-MM-DD，交易日（非自然日）
    # 完整交易日证据（WPD-02）
    observed_symbols: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_symbols: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completeness_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_valid_day: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )  # 是否计入有效观察天数
    invalid_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # incomplete_coverage / data_anomaly / missing_trade_day
    # 当日指标
    ic_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    # 数据健康（WP6-04）
    health_status: Mapped[str] = mapped_column(
        String(24), default="healthy", nullable=False
    )  # healthy/degraded/blocked
    health_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "factor_version_id", "trade_date", name="uq_shadow_observation_version_date"
        ),
        Index("ix_shadow_obs_factor_trade", "factor_id", "trade_date"),
    )


__all__ = [
    "EvaluationRun",
    "TransitionAudit",
    "FactorSet",
    "FactorSetMember",
    "ShadowObservation",
]
