from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FactorVersion(Base):
    __tablename__ = "factor_versions"
    __table_args__ = (
        UniqueConstraint("factor_id", "version", name="uq_factor_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_id: Mapped[int] = mapped_column(
        ForeignKey("factors.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    formula_expr: Mapped[str] = mapped_column(Text)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    direction: Mapped[str] = mapped_column(String(32), default="higher_better")
    source_mapping_json: Mapped[str] = mapped_column(Text, default="{}")
    effective_from: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, index=True
    )
    change_note: Mapped[str] = mapped_column(Text, default="")
    is_latest: Mapped[int] = mapped_column(Integer, default=1, index=True)
    # --- WP1-01: 编译与校验字段 ---
    formula_ast_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    postprocess_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # winsorize/rank/zscore/neutralize/missing policy
    parameter_schema_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_dependencies_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    compiler_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    complexity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_via: Mapped[str | None] = mapped_column(String(32), nullable=True)  # manual/template/ai/import
    ai_provenance_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # model/prompt_hash/output_hash (NO API keys)
    validation_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # pending/valid/invalid
    validation_errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)


class FactorModelRun(Base):
    __tablename__ = "factor_model_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_type: Mapped[str] = mapped_column(String(32), default="ridge", index=True)
    asset_type: Mapped[str] = mapped_column(String(16), default="stock", index=True)
    target_code: Mapped[str] = mapped_column(
        String(64), default="target_5d_return"
    )
    train_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    train_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    validation_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    validation_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_cutoff_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    feature_versions_json: Mapped[str] = mapped_column(Text, default="{}")
    hyperparameters_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    symbol_count: Mapped[int] = mapped_column(Integer, default=0)
    trade_date_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        String(24), default="training", index=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, index=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # Compatibility relationship; current source of truth is the materialized
    # factor_model_members table.
    weights: Mapped[list["FactorModelMember"]] = relationship(
        "FactorModelMember",
        primaryjoin="FactorModelRun.id == foreign(FactorModelMember.model_run_id)",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # ── Task 11 (rev 049): 旧 per-factor FactorWeightSnapshot 表已被
    # factor_weight_snapshots (per-model aggregate, PK=model_id) 取代。
    # 新 ORM 类在 app/models/factor_weight_snapshot.py 中定义，
    # 1:1 映射不再需要 1:many relationship。

# Keep the old per-factor constructor name available to legacy callers. The
# aggregate snapshot is imported directly from factor_weight_snapshot.py.
from app.models.factor_governance import FactorModelMember  # noqa: E402,F401
FactorWeightSnapshot = FactorModelMember

