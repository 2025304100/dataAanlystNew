from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FactorRuntimeState(Base):
    '''Singleton state for the model version used by new decision runs.'''

    __tablename__ = 'factor_runtime_state'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    weight_mode: Mapped[str] = mapped_column(
        String(16), default='manual', index=True
    )
    active_model_run_id: Mapped[str | None] = mapped_column(
        ForeignKey('factor_model_runs.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    updated_by: Mapped[str] = mapped_column(String(128), default='system')
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )


class FactorSystemConfig(Base):
    '''Persistent feature readiness settings managed from the settings page.'''

    __tablename__ = 'factor_system_config'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    feature_enabled: Mapped[int] = mapped_column(Integer, default=0)
    warehouse_path: Mapped[str] = mapped_column(String(1024))
    updated_by: Mapped[str] = mapped_column(String(128), default='environment')
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )


class FactorModelAuditLog(Base):
    '''Append-only audit trail for activation and fallback operations.'''

    __tablename__ = 'factor_model_audit_logs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    model_run_id: Mapped[str | None] = mapped_column(
        ForeignKey('factor_model_runs.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    previous_mode: Mapped[str] = mapped_column(String(16))
    new_mode: Mapped[str] = mapped_column(String(16))
    previous_model_run_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    new_model_run_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    actor: Mapped[str] = mapped_column(String(128), default='local_user')
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, index=True
    )
