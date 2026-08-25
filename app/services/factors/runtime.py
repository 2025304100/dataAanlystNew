'''Persistent activation state and audit operations for factor models.'''
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.factor_model import FactorModelRun
from app.models.factor_runtime import FactorModelAuditLog, FactorRuntimeState


@dataclass(frozen=True)
class FactorRuntimeSnapshot:
    weight_mode: str
    active_model_run_id: str | None
    updated_by: str
    fallback_reason: str | None
    version: int
    updated_at: datetime | None

    @property
    def score_weight_mode(self) -> str:
        if self.weight_mode == 'ridge' and self.active_model_run_id:
            return 'ridge'
        return 'manual'

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            'score_weight_mode': self.score_weight_mode,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _snapshot(state: FactorRuntimeState | None) -> FactorRuntimeSnapshot:
    if state is None:
        configured_mode = settings.factor_weight_mode
        if configured_mode == 'ridge':
            configured_mode = 'manual'
        return FactorRuntimeSnapshot(
            weight_mode=configured_mode,
            active_model_run_id=None,
            updated_by='environment',
            fallback_reason=None,
            version=0,
            updated_at=None,
        )
    return FactorRuntimeSnapshot(
        weight_mode=state.weight_mode,
        active_model_run_id=state.active_model_run_id,
        updated_by=state.updated_by,
        fallback_reason=state.fallback_reason,
        version=state.version,
        updated_at=state.updated_at,
    )


def get_factor_runtime_snapshot(db: Session) -> FactorRuntimeSnapshot:
    return _snapshot(db.get(FactorRuntimeState, 1))


def ensure_factor_runtime_state(db: Session) -> FactorRuntimeState:
    state = db.get(FactorRuntimeState, 1)
    if state is None:
        initial_mode = settings.factor_weight_mode
        if initial_mode == 'ridge':
            initial_mode = 'manual'
        state = FactorRuntimeState(
            id=1,
            weight_mode=initial_mode,
            active_model_run_id=None,
            updated_by='environment',
            version=1,
        )
        db.add(state)
        db.flush()
    return state


def activate_factor_model(
    db: Session,
    model_run_id: str,
    *,
    mode: str,
    actor: str = 'local_user',
    note: str | None = None,
) -> FactorRuntimeSnapshot:
    if mode not in {'shadow', 'ridge'}:
        raise ValueError('activation mode must be shadow or ridge')
    model = db.get(FactorModelRun, model_run_id)
    if model is None:
        raise ValueError(f'factor model does not exist: {model_run_id}')
    if model.status != 'validated':
        raise ValueError(
            f'only validated models can be activated, got {model.status}'
        )
    state = db.execute(
        select(FactorRuntimeState)
        .where(FactorRuntimeState.id == 1)
        .with_for_update()
    ).scalar_one_or_none()
    if state is None:
        state = ensure_factor_runtime_state(db)
    previous_mode = state.weight_mode
    previous_model = state.active_model_run_id
    state.weight_mode = mode
    state.active_model_run_id = model.id
    state.updated_by = actor
    state.fallback_reason = None
    state.version += 1
    state.updated_at = _now()
    model.activated_at = _now()
    db.add(
        FactorModelAuditLog(
            action='activate',
            model_run_id=model.id,
            previous_mode=previous_mode,
            new_mode=mode,
            previous_model_run_id=previous_model,
            new_model_run_id=model.id,
            actor=actor,
            note=note,
        )
    )
    db.flush()
    return _snapshot(state)


def fallback_factor_model(
    db: Session,
    *,
    actor: str = 'local_user',
    reason: str,
) -> FactorRuntimeSnapshot:
    if not reason.strip():
        raise ValueError('fallback reason is required')
    state = db.execute(
        select(FactorRuntimeState)
        .where(FactorRuntimeState.id == 1)
        .with_for_update()
    ).scalar_one_or_none()
    if state is None:
        state = ensure_factor_runtime_state(db)
    previous_mode = state.weight_mode
    previous_model = state.active_model_run_id
    state.weight_mode = 'manual'
    state.active_model_run_id = None
    state.updated_by = actor
    state.fallback_reason = reason.strip()
    state.version += 1
    state.updated_at = _now()
    db.add(
        FactorModelAuditLog(
            action='fallback',
            model_run_id=previous_model,
            previous_mode=previous_mode,
            new_mode='manual',
            previous_model_run_id=previous_model,
            new_model_run_id=None,
            actor=actor,
            note=reason.strip(),
        )
    )
    db.flush()
    return _snapshot(state)


def retire_factor_model(
    db: Session,
    model_run_id: str,
    *,
    actor: str = 'local_user',
    reason: str,
) -> FactorRuntimeSnapshot:
    """Retire a released model and remove it from all new live selections.

    Retiring the current global model is deliberately a fail-closed operation:
    it clears the runtime pointer and switches the score mode to ``manual``.
    Existing immutable snapshots remain historical evidence; they cannot be
    selected again by the strategy-binding service after retirement.
    """
    if not reason or not reason.strip():
        raise ValueError('retirement reason is required')
    model = db.get(FactorModelRun, model_run_id)
    if model is None:
        raise ValueError(f'factor model does not exist: {model_run_id}')

    state = db.execute(
        select(FactorRuntimeState)
        .where(FactorRuntimeState.id == 1)
        .with_for_update()
    ).scalar_one_or_none()
    if state is None:
        state = ensure_factor_runtime_state(db)

    if model.status == 'retired':
        return _snapshot(state)

    previous_mode = state.weight_mode
    previous_model = state.active_model_run_id
    model.status = 'retired'
    model.rejection_reason = f'retired: {reason.strip()}'

    if state.active_model_run_id == model_run_id:
        state.weight_mode = 'manual'
        state.active_model_run_id = None
        state.fallback_reason = f'retired:{model_run_id}:{reason.strip()}'
        state.version += 1
        state.updated_at = _now()

    db.add(
        FactorModelAuditLog(
            action='retire',
            model_run_id=model_run_id,
            previous_mode=previous_mode,
            new_mode=state.weight_mode,
            previous_model_run_id=previous_model,
            new_model_run_id=state.active_model_run_id,
            actor=actor,
            note=reason.strip(),
        )
    )
    db.flush()
    return _snapshot(state)


__all__ = [
    'FactorRuntimeSnapshot',
    'activate_factor_model',
    'ensure_factor_runtime_state',
    'fallback_factor_model',
    'get_factor_runtime_snapshot',
    'retire_factor_model',
]
