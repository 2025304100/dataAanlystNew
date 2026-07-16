from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.health import get_factor_health
from app.services.factors.config import (
    get_factor_system_config,
    update_factor_system_config,
)
from app.services.factors.runtime import get_factor_runtime_snapshot
from app.services.factors.store import FactorWarehouse


router = APIRouter()


class FactorSystemConfigUpdate(BaseModel):
    feature_enabled: bool
    actor: str = Field(default='local_user', min_length=1, max_length=128)


def _factor_overview(db: Session) -> dict:
    config = get_factor_system_config(db)
    health = get_factor_health(
        FactorWarehouse(config.warehouse_path)
    ).to_dict()
    reasons = health.get('reasons') or []
    return {
        'runtime': get_factor_runtime_snapshot(db).to_dict(),
        'config': config.to_dict(),
        'feature_enabled': config.feature_enabled,
        'warehouse_error': reasons[0] if reasons else None,
        'health': health,
        'latest_trade_date': health.get('latest_bar_date'),
        'factor_coverage': health.get('factors', []),
    }


@router.get('/factors/overview')
def get_factors_overview(db: Session = Depends(get_db)):
    return _factor_overview(db)


@router.get('/factors/config')
def get_factor_config(db: Session = Depends(get_db)):
    return get_factor_system_config(db).to_dict()


@router.patch('/factors/config')
def patch_factor_config(
    payload: FactorSystemConfigUpdate,
    db: Session = Depends(get_db),
):
    try:
        snapshot = update_factor_system_config(
            db,
            feature_enabled=payload.feature_enabled,
            actor=payload.actor,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return snapshot.to_dict()


@router.post('/factors/warehouse/initialize')
def initialize_factor_warehouse(db: Session = Depends(get_db)):
    config = get_factor_system_config(db)
    if not config.feature_enabled:
        raise HTTPException(
            status_code=409,
            detail='Enable the factor feature before initializing the warehouse',
        )
    try:
        FactorWarehouse(config.warehouse_path).initialize()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _factor_overview(db)


@router.get('/factors')
def list_factors(db: Session = Depends(get_db)):
    rows = db.execute(select(Factor).order_by(Factor.category, Factor.code)).scalars().all()
    return [
        {
            'code': row.code,
            'name': row.name,
            'category': row.category,
            'direction': row.direction,
            'status': row.status,
            'source_type': row.source_type,
            'frequency': row.frequency,
            'default_missing_policy': row.default_missing_policy,
            'is_active': bool(row.is_active),
            'description': row.description,
            'formula_expr': row.formula_expr,
        }
        for row in rows
    ]


@router.get('/factors/{factor_code}/versions')
def list_factor_versions(
    factor_code: str,
    db: Session = Depends(get_db),
):
    factor = db.execute(
        select(Factor).where(Factor.code == factor_code)
    ).scalar_one_or_none()
    if factor is None:
        raise HTTPException(status_code=404, detail='Factor not found')
    versions = db.execute(
        select(FactorVersion)
        .where(FactorVersion.factor_id == factor.id)
        .order_by(desc(FactorVersion.version))
    ).scalars().all()
    return [
        {
            'factor_code': factor.code,
            'version': item.version,
            'formula_expr': item.formula_expr,
            'params': json.loads(item.params_json or '{}'),
            'direction': item.direction,
            'source_mapping': json.loads(item.source_mapping_json or '{}'),
            'effective_from': item.effective_from,
            'change_note': item.change_note,
            'is_latest': bool(item.is_latest),
            'created_at': item.created_at,
        }
        for item in versions
    ]


@router.get('/factors/symbols/{symbol_id}/explanation')
def get_symbol_factor_explanation(
    symbol_id: int,
    trade_date: date | None = Query(default=None),
    model_run_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail='Symbol not found')
    runtime = get_factor_runtime_snapshot(db)
    effective_model_id = model_run_id or runtime.active_model_run_id
    stmt = select(Score).where(Score.symbol_id == symbol_id)
    if trade_date is not None:
        stmt = stmt.where(Score.trade_date == trade_date)
    if effective_model_id:
        stmt = stmt.where(
            Score.factor_model_run_id == effective_model_id,
            Score.weight_mode.in_(('shadow', 'ridge')),
        )
    else:
        stmt = stmt.where(Score.weight_mode.in_(('shadow', 'ridge')))
    score = db.execute(
        stmt.order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()
    if score is None:
        raise HTTPException(
            status_code=404,
            detail='Dynamic factor explanation not found',
        )
    try:
        factor_scores = json.loads(score.factor_scores_json or '{}')
    except (TypeError, json.JSONDecodeError):
        factor_scores = {}
    explanation = factor_scores.get('_dynamic_model')
    if not isinstance(explanation, dict):
        raise HTTPException(
            status_code=404,
            detail='Dynamic factor explanation not found',
        )
    return {
        'symbol_id': symbol.id,
        'symbol': symbol.symbol,
        'name': symbol.name,
        'trade_date': score.trade_date,
        'weight_mode': score.weight_mode,
        'model_run_id': score.factor_model_run_id,
        'factor_data_cutoff_at': score.factor_data_cutoff_at,
        'factor_quality_score': score.factor_quality_score,
        'factor_timing_score': score.factor_timing_score,
        'model_alpha_score': score.model_alpha_score,
        'macro_regime': score.macro_regime,
        'macro_position_multiplier': score.macro_position_multiplier,
        'explanation': explanation,
    }
