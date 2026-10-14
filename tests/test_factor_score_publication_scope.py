"""Publication scopes use the market cross-section and restrict Score writes."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import select

pytest.importorskip('duckdb')

from app.models.factor_governance import FactorModelMember, ScoringLineage
from app.models.factor_model import FactorModelRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.scoring_bridge import (
    materialize_factor_scores,
    materialize_factor_score_window,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox

DAY = date(2026, 10, 13)
CUTOFF = datetime(2026, 10, 13, 16)
BATCH = 'scope-factor-batch'
MODEL = 'scope-publication-model'


def _seed(db, warehouse, *, dates=(DAY,), unmapped=False):
    model = FactorModelRun(
        id=MODEL, model_type='ridge', status='validated', asset_type='stock',
        target_code='target_5d_return', data_cutoff_at=CUTOFF,
        sample_count=1000, symbol_count=100, trade_date_count=250,
    )
    model.weights.append(FactorModelMember(
        factor_code='ep_ttm', factor_version=1,
        coefficient=1.0, normalized_weight=1.0,
    ))
    db.add(model)
    for code in ('000001', '000002', '000003', '000004'):
        db.add(Symbol(symbol=code, name=code, asset_type='stock', market='CN', is_active=1))
    db.flush()
    codes = ['000001', '000002', '000003']
    if unmapped:
        codes.append('009999')
    warehouse.upsert_records('factor_values', [
        {
            'symbol': code, 'trade_date': day, 'factor_code': 'ep_ttm',
            'factor_version': 1, 'raw_value': float(index),
            'winsorized_value': float(index), 'normalized_value': float(index),
            'is_imputed': False, 'imputation_method': None, 'eligible': True,
            'data_cutoff_at': CUTOFF, 'calc_batch_id': BATCH, 'created_at': CUTOFF,
        }
        for day in dates for index, code in enumerate(codes)
    ])


def _publish(db, warehouse, **kwargs):
    return materialize_factor_scores(
        db, warehouse, trade_date=DAY, factor_calc_batch_id=BATCH,
        model_run_id=MODEL, mode='ridge', **kwargs,
    )


def _scores(db, batch_id):
    return dict(db.execute(
        select(Symbol.symbol, Score).join(Symbol, Symbol.id == Score.symbol_id)
        .where(Score.calc_batch_id == batch_id)
    ).all())


def test_selected_publication_keeps_full_market_percentiles(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'scoped.duckdb')
    _seed(db_session, warehouse)
    selected = _publish(db_session, warehouse, target_symbols=('000003',))
    selected_scores = _scores(db_session, selected.calc_batch_id)

    assert set(selected_scores) == {'000003'}
    assert selected.materialized_count == 1
    assert selected.skipped_symbol_count == 0
    assert selected.requested_symbol_count == 1
    assert selected.scoped_symbol_count == 1
    assert selected.scope_excluded_symbol_count == 2
    assert selected_scores['000003'].model_alpha_score == 100.0

    full = _publish(db_session, warehouse)
    full_scores = _scores(db_session, full.calc_batch_id)
    assert set(full_scores) == {'000001', '000002', '000003'}
    assert selected.calc_batch_id != full.calc_batch_id
    for field in ('quality_score', 'timing_score', 'priority_score',
                  'factor_quality_score', 'factor_timing_score', 'model_alpha_score'):
        assert getattr(selected_scores['000003'], field) == getattr(full_scores['000003'], field)


def test_only_in_scope_missing_inputs_and_mappings_affect_counts(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'missing.duckdb')
    _seed(db_session, warehouse, unmapped=True)
    selected = _publish(db_session, warehouse, target_symbols=('000003', '000004', '000004'))

    assert set(_scores(db_session, selected.calc_batch_id)) == {'000003'}
    assert selected.requested_symbol_count == 2
    assert selected.scoped_symbol_count == 1
    assert selected.missing_factor_count == 1
    assert selected.missing_symbol_mapping_count == 0
    assert selected.skipped_symbol_count == 1
    assert selected.scope_excluded_symbol_count == 3
    lineage = db_session.execute(select(ScoringLineage).where(
        ScoringLineage.score_batch_id == selected.calc_batch_id,
    )).scalar_one()
    evidence = json.loads(lineage.stats_json)
    assert evidence['target_symbols'] == ['000003', '000004']
    assert evidence['cross_section_symbol_count'] == 4
    assert evidence['missing_factor_count'] == 1


def test_scope_retry_does_not_mutate_other_symbols_existing_scores(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'retry.duckdb')
    _seed(db_session, warehouse)
    full = _publish(db_session, warehouse)
    original = {code: (score.id, score.published_at) for code, score in _scores(db_session, full.calc_batch_id).items()}
    selected = _publish(db_session, warehouse, target_symbols=('000003', '000002'))
    retry = _publish(db_session, warehouse, target_symbols=('000002', '000003', '000003'))

    assert selected.calc_batch_id == retry.calc_batch_id
    assert len(_scores(db_session, selected.calc_batch_id)) == 2
    assert {code: (score.id, score.published_at) for code, score in _scores(db_session, full.calc_batch_id).items()} == original


def test_empty_scope_never_falls_back_to_full_publication(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'empty.duckdb')
    _seed(db_session, warehouse)
    result = _publish(db_session, warehouse, target_symbols=())
    assert result.materialized_count == 0
    assert result.requested_symbol_count == 0
    assert result.calc_batch_id is None
    assert db_session.execute(select(Score)).scalars().all() == []
    assert db_session.execute(select(ScoringLineage)).scalars().all() == []


def test_window_propagates_scope_and_excludes_outside_missing_mapping(db_session, tmp_path):
    second = date(2026, 10, 14)
    warehouse = FactorWarehouse(tmp_path / 'window.duckdb')
    _seed(db_session, warehouse, dates=(DAY, second), unmapped=True)
    result = materialize_factor_score_window(
        db_session, warehouse, trade_dates=[DAY, second], factor_calc_batch_id=BATCH,
        model_run_id=MODEL, mode='ridge', target_symbols=('000003',),
    )
    assert result['status'] == 'PUBLISHED'
    assert result['materialized_count'] == 2
    assert result['requested_symbol_count'] == 1
    assert result['missing_symbol_mapping_count'] == 0
    assert result['skipped_symbol_count'] == 0
    assert set(db_session.execute(select(Symbol.symbol).join(Score, Score.symbol_id == Symbol.id)).scalars()) == {'000003'}
    assert all(item['requested_symbol_count'] == 1 for item in result['daily_batches'])


def test_window_is_partial_only_for_missing_requested_symbols(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'partial-window.duckdb')
    _seed(db_session, warehouse)
    result = materialize_factor_score_window(
        db_session, warehouse, trade_dates=[DAY], factor_calc_batch_id=BATCH,
        model_run_id=MODEL, mode='ridge', target_symbols=('000003', '000004'),
    )
    assert result['status'] == 'PARTIAL'
    assert result['materialized_count'] == 1
    assert result['missing_factor_count'] == 1
    assert result['skipped_symbol_count'] == 1


def test_empty_window_scope_is_explicitly_skipped(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'empty-window.duckdb')
    _seed(db_session, warehouse)
    result = materialize_factor_score_window(
        db_session, warehouse, trade_dates=[DAY], factor_calc_batch_id=BATCH,
        model_run_id=MODEL, mode='ridge', target_symbols=(),
    )
    assert result['status'] == 'SKIPPED'
    assert result['reason'] == 'empty_publication_scope'
    assert result['materialized_count'] == 0
    assert result['requested_symbol_count'] == 0
    assert db_session.execute(select(Score)).scalars().all() == []
