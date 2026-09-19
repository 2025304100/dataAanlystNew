from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.router import api_router
from app.api.routes.factor_models import (
    FactorModelActivationRequest,
    FactorModelFallbackRequest,
    activate_model,
    fallback_model,
    get_factor_model,
    list_factor_models,
)
from app.api.routes.factors import get_symbol_factor_explanation
from app.models.factor_model import FactorModelRun, FactorWeightSnapshot
from app.models.factor_runtime import (
    FactorModelAuditLog,
    FactorRuntimeState,
    FactorSystemConfig,
)
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.ridge_model import FEATURE_CODES
from app.services.factors.runtime import (
    activate_factor_model,
    fallback_factor_model,
    get_factor_runtime_snapshot,
)
from app.schemas.async_task import FactorPipelineCreate
from app.services.factors.pipeline_task import create_factor_pipeline_task

pytestmark = pytest.mark.whitebox


def _add_model(db_session, model_id: str, status: str = 'validated'):
    model = FactorModelRun(
        id=model_id,
        model_type='ridge',
        asset_type='stock',
        target_code='target_5d_return',
        status=status,
        sample_count=1000,
        symbol_count=100,
        trade_date_count=250,
    )
    for index, code in enumerate(FEATURE_CODES, start=1):
        coefficient = index / 10
        model.weights.append(
            FactorWeightSnapshot(
                factor_code=code,
                factor_version=1,
                coefficient=coefficient,
                normalized_weight=coefficient / 1.0,
            )
        )
    db_session.add(model)
    db_session.flush()
    return model


def test_runtime_activation_and_fallback_are_persistent_and_audited(db_session):
    model = _add_model(db_session, 'runtime-model')
    initial = get_factor_runtime_snapshot(db_session)
    assert initial.weight_mode == 'manual'
    assert initial.active_model_run_id is None

    shadow = activate_factor_model(
        db_session,
        model.id,
        mode='shadow',
        actor='tester',
        note='observe first',
    )
    ridge = activate_factor_model(
        db_session,
        model.id,
        mode='ridge',
        actor='tester',
        note='promote after review',
    )
    manual = fallback_factor_model(
        db_session,
        actor='tester',
        reason='validation drift',
    )
    db_session.commit()

    assert shadow.weight_mode == 'shadow'
    assert ridge.score_weight_mode == 'ridge'
    assert manual.weight_mode == 'manual'
    assert manual.active_model_run_id is None
    state = db_session.get(FactorRuntimeState, 1)
    assert state.fallback_reason == 'validation drift'
    assert model.activated_at is not None
    assert db_session.scalar(
        select(func.count()).select_from(FactorModelAuditLog)
    ) == 3


def test_rejected_model_cannot_be_activated(db_session):
    model = _add_model(db_session, 'rejected-runtime', status='rejected')
    with pytest.raises(ValueError, match='validated'):
        activate_factor_model(
            db_session,
            model.id,
            mode='ridge',
        )


def test_factor_model_routes_activate_list_detail_and_fallback(db_session):
    model = _add_model(db_session, 'api-model')

    activated = activate_model(
        model.id,
        FactorModelActivationRequest(
            mode='shadow',
            actor='api-test',
            note='shadow acceptance',
        ),
        db_session,
    )
    listed = list_factor_models(status=None, limit=20, db=db_session)
    detail = get_factor_model(model.id, db_session)
    fallback = fallback_model(
        FactorModelFallbackRequest(
            actor='api-test',
            reason='manual rollback',
        ),
        db_session,
    )

    assert activated['weight_mode'] == 'shadow'
    assert listed['items'][0]['id'] == model.id
    assert len(detail['weights']) == len(FEATURE_CODES)
    assert detail['audit'][0]['action'] == 'activate'
    assert fallback['weight_mode'] == 'manual'


def test_symbol_explanation_route_reads_immutable_score_snapshot(db_session):
    symbol = Symbol(
        symbol='600000',
        name='Test',
        asset_type='stock',
        market='sh',
        is_active=1,
    )
    db_session.add(symbol)
    db_session.flush()
    explanation = {
        'model_run_id': 'explain-model',
        'factors': {
            'ep_ttm': {
                'raw_value': 0.1,
                'winsorized_value': 0.1,
                'normalized_value': 1.2,
                'factor_version': 1,
                'coefficient': 0.3,
                'contribution': 0.36,
                'is_imputed': False,
            }
        },
        'macro': {'regime': 'neutral'},
    }
    db_session.add(
        Score(
            symbol_id=symbol.id,
            trade_date=date(2026, 1, 5),
            quality_score=70,
            quality_grade='B',
            timing_score=65,
            stage='start',
            action='open',
            priority_score=68,
            weight_mode='shadow',
            factor_model_run_id='explain-model',
            factor_scores_json=json.dumps(
                {'_dynamic_model': explanation}
            ),
            calc_batch_id='explanation-test',
        )
    )
    db_session.flush()

    response = get_symbol_factor_explanation(
        symbol.id,
        trade_date=date(2026, 1, 5),
        model_run_id='explain-model',
        db=db_session,
    )
    assert response['model_run_id'] == 'explain-model'
    assert response['explanation']['factors']['ep_ttm']['contribution'] == 0.36

    with pytest.raises(HTTPException) as exc:
        get_symbol_factor_explanation(
            symbol.id,
            trade_date=date(2026, 1, 6),
            model_run_id='explain-model',
            db=db_session,
        )
    assert exc.value.status_code == 404


def test_factor_routes_are_registered():
    paths = {route.path for route in api_router.routes}
    assert '/api/v1/factors/overview' in paths
    assert '/api/v1/factors/symbols/{symbol_id}/explanation' in paths
    assert '/api/v1/factor-models/{model_run_id}/activate' in paths
    assert '/api/v1/factor-models/fallback' in paths
    assert '/api/v1/factor-pipeline/tasks' in paths


def test_factor_pipeline_respects_feature_flag(db_session):
    db_session.add(
        FactorSystemConfig(
            id=1,
            feature_enabled=0,
            warehouse_path='factor.duckdb',
            updated_by='test',
        )
    )
    db_session.commit()
    with pytest.raises(ValueError, match='disabled'):
        create_factor_pipeline_task(FactorPipelineCreate())
