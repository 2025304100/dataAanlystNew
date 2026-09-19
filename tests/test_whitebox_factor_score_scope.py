from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.api.routes.dashboard import _latest_score_map
from app.services.backtest import _build_score_map
from app.models.factor_model import FactorModelRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.runtime import (
    activate_factor_model,
    fallback_factor_model,
)
from app.services.factors.score_scope import apply_active_score_scope
from app.services.trade_plans import get_latest_score


def _score(symbol_id: int, *, mode: str, priority: float, model: str | None):
    return Score(
        symbol_id=symbol_id,
        trade_date=date(2026, 1, 5),
        quality_score=priority,
        quality_grade='B',
        timing_score=priority,
        stage='start',
        action='open',
        priority_score=priority,
        weight_mode=mode,
        factor_model_run_id=model,
        calc_batch_id=f'{mode}-{model or "none"}',
    )


def test_all_decision_queries_follow_runtime_mode_and_model(db_session):
    symbol = Symbol(
        symbol='000001',
        name='Scope',
        asset_type='stock',
        market='sz',
        is_active=1,
    )
    model = FactorModelRun(
        id='active-scope-model',
        status='validated',
        model_type='ridge',
        asset_type='stock',
        target_code='target_5d_return',
    )
    other = FactorModelRun(
        id='other-scope-model',
        status='validated',
        model_type='ridge',
        asset_type='stock',
        target_code='target_5d_return',
    )
    db_session.add_all([symbol, model, other])
    db_session.flush()
    manual = _score(
        symbol.id, mode='manual', priority=40, model=None
    )
    shadow = _score(
        symbol.id,
        mode='shadow',
        priority=90,
        model=model.id,
    )
    ridge = _score(
        symbol.id,
        mode='ridge',
        priority=70,
        model=model.id,
    )
    wrong_ridge = _score(
        symbol.id,
        mode='ridge',
        priority=99,
        model=other.id,
    )
    db_session.add_all([manual, shadow, ridge, wrong_ridge])
    db_session.flush()

    assert get_latest_score(db_session, symbol.id).id == manual.id
    assert _latest_score_map(db_session, [symbol.id])[symbol.id].id == manual.id

    activate_factor_model(
        db_session,
        model.id,
        mode='shadow',
        actor='scope-test',
    )
    assert get_latest_score(db_session, symbol.id).id == manual.id

    activate_factor_model(
        db_session,
        model.id,
        mode='ridge',
        actor='scope-test',
    )
    selected = db_session.execute(
        apply_active_score_scope(
            select(Score).where(Score.symbol_id == symbol.id),
            db_session,
        )
    ).scalars().all()
    assert [item.id for item in selected] == [ridge.id]
    assert get_latest_score(db_session, symbol.id).id == ridge.id
    assert _latest_score_map(db_session, [symbol.id])[symbol.id].id == ridge.id
    pinned = _build_score_map(
        db_session,
        [symbol.id],
        date(2026, 1, 1),
        date(2026, 1, 10),
        score_weight_mode='ridge',
        factor_model_run_id=model.id,
    )
    assert [item.id for item in pinned[symbol.id]] == [ridge.id]

    fallback_factor_model(
        db_session,
        actor='scope-test',
        reason='test fallback',
    )
    assert get_latest_score(db_session, symbol.id).id == manual.id
