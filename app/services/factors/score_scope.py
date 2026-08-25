'''Shared query scope for the currently active Score batch family.'''
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import false
from sqlalchemy.sql import Select
from sqlalchemy.orm import Session

from app.models.factor_model import FactorModelRun
from app.models.score import Score
from app.services.factors.runtime import get_factor_runtime_snapshot


@dataclass(frozen=True)
class ActiveScoreScope:
    weight_mode: str
    model_run_id: str | None
    model_ready: bool = True


def get_active_score_scope(db: Session) -> ActiveScoreScope:
    runtime = get_factor_runtime_snapshot(db)
    if runtime.score_weight_mode == 'ridge':
        model = db.get(FactorModelRun, runtime.active_model_run_id)
        if model is None or model.status != 'validated':
            # Do not silently fall back to manual Scores.  A retired/corrupt
            # runtime pointer must be visible as an empty dynamic scope so a
            # caller cannot accidentally trade on a different interpretation.
            return ActiveScoreScope(
                weight_mode='ridge',
                model_run_id=runtime.active_model_run_id,
                model_ready=False,
            )
    return ActiveScoreScope(
        weight_mode=runtime.score_weight_mode,
        model_run_id=(
            runtime.active_model_run_id
            if runtime.score_weight_mode == 'ridge'
            else None
        ),
    )


def apply_active_score_scope(
    statement: Select[Any],
    db: Session,
) -> Select[Any]:
    scope = get_active_score_scope(db)
    if not scope.model_ready:
        return statement.where(false())
    statement = statement.where(Score.weight_mode == scope.weight_mode)
    if scope.weight_mode == 'ridge':
        statement = statement.where(
            Score.factor_model_run_id == scope.model_run_id
        )
    return statement


__all__ = [
    'ActiveScoreScope',
    'apply_active_score_scope',
    'get_active_score_scope',
]
