'''Persistent factor feature configuration and readiness helpers.'''
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_session_local
from app.models.factor_runtime import FactorRuntimeState, FactorSystemConfig


@dataclass(frozen=True)
class FactorSystemConfigSnapshot:
    feature_enabled: bool
    warehouse_path: str
    updated_by: str
    updated_at: datetime | None

    def to_dict(self) -> dict:
        return asdict(self)


def _environment_snapshot() -> FactorSystemConfigSnapshot:
    return FactorSystemConfigSnapshot(
        feature_enabled=bool(settings.factor_feature_enabled),
        warehouse_path=str(settings.factor_warehouse_path),
        updated_by='environment',
        updated_at=None,
    )


def _snapshot(row: FactorSystemConfig | None) -> FactorSystemConfigSnapshot:
    if row is None:
        return _environment_snapshot()
    return FactorSystemConfigSnapshot(
        feature_enabled=bool(row.feature_enabled),
        warehouse_path=row.warehouse_path,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def ensure_factor_system_config(db: Session) -> FactorSystemConfig:
    row = db.get(FactorSystemConfig, 1)
    if row is None:
        initial = _environment_snapshot()
        row = FactorSystemConfig(
            id=1,
            feature_enabled=int(initial.feature_enabled),
            warehouse_path=initial.warehouse_path,
            updated_by=initial.updated_by,
        )
        db.add(row)
        db.flush()
    return row


def get_factor_system_config(db: Session) -> FactorSystemConfigSnapshot:
    return _snapshot(db.get(FactorSystemConfig, 1))


def get_current_factor_system_config() -> FactorSystemConfigSnapshot:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        return get_factor_system_config(db)
    finally:
        db.close()


def update_factor_system_config(
    db: Session,
    *,
    feature_enabled: bool,
    actor: str,
) -> FactorSystemConfigSnapshot:
    row = ensure_factor_system_config(db)
    if not feature_enabled:
        runtime = db.get(FactorRuntimeState, 1)
        if runtime is not None and runtime.weight_mode != 'manual':
            raise ValueError(
                'Fallback to manual mode before disabling the factor feature'
            )
    row.feature_enabled = int(feature_enabled)
    row.updated_by = actor
    db.flush()
    return _snapshot(row)


__all__ = [
    'FactorSystemConfigSnapshot',
    'ensure_factor_system_config',
    'get_current_factor_system_config',
    'get_factor_system_config',
    'update_factor_system_config',
]
