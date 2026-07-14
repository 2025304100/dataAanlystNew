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

    weights = relationship(
        "FactorWeightSnapshot",
        back_populates="model_run",
        cascade="all, delete-orphan",
    )


class FactorWeightSnapshot(Base):
    __tablename__ = "factor_weight_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "model_run_id",
            "factor_code",
            "factor_version",
            name="uq_factor_model_weight",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[str] = mapped_column(
        ForeignKey("factor_model_runs.id", ondelete="CASCADE"), index=True
    )
    factor_code: Mapped[str] = mapped_column(String(64), index=True)
    factor_version: Mapped[int] = mapped_column(Integer)
    coefficient: Mapped[float] = mapped_column(Float)
    normalized_weight: Mapped[float] = mapped_column(Float)
    train_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)

    model_run = relationship("FactorModelRun", back_populates="weights")

