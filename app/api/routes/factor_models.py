from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.factor_model import FactorModelRun
from app.models.factor_runtime import FactorModelAuditLog
from app.services.factors.runtime import (
    activate_factor_model,
    fallback_factor_model,
    get_factor_runtime_snapshot,
)


router = APIRouter()


class FactorModelActivationRequest(BaseModel):
    mode: str = Field(default='shadow', pattern='^(shadow|ridge)$')
    actor: str = Field(default='local_user', min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


class FactorModelFallbackRequest(BaseModel):
    actor: str = Field(default='local_user', min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _model_view(model: FactorModelRun, *, include_audit: list | None = None) -> dict:
    result = {
        'id': model.id,
        'model_type': model.model_type,
        'asset_type': model.asset_type,
        'target_code': model.target_code,
        'train_start_date': model.train_start_date,
        'train_end_date': model.train_end_date,
        'validation_start_date': model.validation_start_date,
        'validation_end_date': model.validation_end_date,
        'data_cutoff_at': model.data_cutoff_at,
        'feature_versions': _json(model.feature_versions_json),
        'hyperparameters': _json(model.hyperparameters_json),
        'metrics': _json(model.metrics_json),
        'sample_count': model.sample_count,
        'symbol_count': model.symbol_count,
        'trade_date_count': model.trade_date_count,
        'status': model.status,
        'rejection_reason': model.rejection_reason,
        'artifact_path': model.artifact_path,
        'created_at': model.created_at,
        'activated_at': model.activated_at,
        'weights': [
            {
                'factor_code': weight.factor_code,
                'factor_version': weight.factor_version,
                'coefficient': weight.coefficient,
                'normalized_weight': weight.normalized_weight,
                'train_ic': weight.train_ic,
                'validation_ic': weight.validation_ic,
            }
            for weight in sorted(
                model.weights, key=lambda item: item.factor_code
            )
        ],
    }
    if include_audit is not None:
        result['audit'] = include_audit
    return result


def _audit_view(item: FactorModelAuditLog) -> dict:
    return {
        'id': item.id,
        'action': item.action,
        'model_run_id': item.model_run_id,
        'previous_mode': item.previous_mode,
        'new_mode': item.new_mode,
        'previous_model_run_id': item.previous_model_run_id,
        'new_model_run_id': item.new_model_run_id,
        'actor': item.actor,
        'note': item.note,
        'created_at': item.created_at,
    }


@router.get('/factor-models/runtime')
def get_factor_model_runtime(db: Session = Depends(get_db)):
    return get_factor_runtime_snapshot(db).to_dict()


@router.get('/factor-models/latest')
def get_latest_factor_model(db: Session = Depends(get_db)):
    model = db.execute(
        select(FactorModelRun).order_by(
            desc(FactorModelRun.created_at), desc(FactorModelRun.id)
        )
    ).scalars().first()
    if model is None:
        raise HTTPException(status_code=404, detail='Factor model not found')
    return _model_view(model)


@router.get('/factor-models')
def list_factor_models(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(FactorModelRun).order_by(
        desc(FactorModelRun.created_at), desc(FactorModelRun.id)
    )
    if status is not None:
        stmt = stmt.where(FactorModelRun.status == status)
    rows = db.execute(stmt.limit(limit)).scalars().all()
    runtime = get_factor_runtime_snapshot(db)
    return {
        'runtime': runtime.to_dict(),
        'items': [_model_view(item) for item in rows],
    }


@router.get('/factor-models/{model_run_id}')
def get_factor_model(
    model_run_id: str,
    db: Session = Depends(get_db),
):
    model = db.get(FactorModelRun, model_run_id)
    if model is None:
        raise HTTPException(status_code=404, detail='Factor model not found')
    audit = db.execute(
        select(FactorModelAuditLog)
        .where(
            (FactorModelAuditLog.model_run_id == model_run_id)
            | (FactorModelAuditLog.previous_model_run_id == model_run_id)
            | (FactorModelAuditLog.new_model_run_id == model_run_id)
        )
        .order_by(desc(FactorModelAuditLog.created_at))
        .limit(50)
    ).scalars().all()
    return _model_view(model, include_audit=[_audit_view(item) for item in audit])


@router.post('/factor-models/{model_run_id}/activate')
def activate_model(
    model_run_id: str,
    payload: FactorModelActivationRequest,
    db: Session = Depends(get_db),
):
    try:
        snapshot = activate_factor_model(
            db,
            model_run_id,
            mode=payload.mode,
            actor=payload.actor,
            note=payload.note,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snapshot.to_dict()


@router.post('/factor-models/fallback')
def fallback_model(
    payload: FactorModelFallbackRequest,
    db: Session = Depends(get_db),
):
    try:
        snapshot = fallback_factor_model(
            db,
            actor=payload.actor,
            reason=payload.reason,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snapshot.to_dict()
