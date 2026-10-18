"""Two genuine turnover bases through isolated public training and consumers.

Only thread dispatch is synchronous (shared fixture). SQL mirrors, the public
formula registry/freeze, targets, eligibility gates, Ridge fit, input snapshot,
native model read and scoring bridge are production implementations. Every
write and activation belongs to temporary pytest SQLite/DuckDB stores.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from tests.test_turnover_v3_public_warehouse_lifecycle import turnover_client
from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market

pytestmark = pytest.mark.whitebox


CONTRACT = json.loads((Path(__file__).resolve().parents[1] /
    '.analysis/turnover-phase2-quadratic-contract-2026-10-16.json').read_text(encoding='utf-8'))
BASE = 'turnover_z20'
SQUARED = 'turnover_z20_squared'


def _seed_quadratic_market(db, tmp_path, *, asymmetric):
    from app.models.factor_runtime import FactorSystemConfig
    from app.models.universe import UniverseDailyBar, UniverseSymbol
    from app.services.factors.definitions import seed_factor_definitions

    seed_factor_definitions(db)
    path, days, symbol_ids, watchlist_id = _seed_local_market(db, tmp_path, day_count=130)
    config = db.get(FactorSystemConfig, 1)
    # Keep real gate execution; synthetic learnable fixtures need a permissive
    # IC upper bound already set by the shared isolated fixture. A real positive
    # floor makes a failed member observable independently of the second basis.
    gates = json.loads(config.extra_json)
    gates['gates']['ic_min'] = .01
    config.extra_json = json.dumps(gates)
    for index, universe in enumerate(db.scalars(select(UniverseSymbol).order_by(UniverseSymbol.symbol))):
        slope = index % 3 - 1
        # Asymmetric outcomes require both the signed and squared basis.
        # Symmetric outcomes make only the square admissible; the signed base
        # must still fail admission and block this multi-feature candidate.
        return_signal = slope + 2 * slope * slope if asymmetric else slope * slope
        previous = 10.0
        bars = db.scalars(select(UniverseDailyBar).where(
            UniverseDailyBar.universe_symbol_id == universe.id,
        ).order_by(UniverseDailyBar.trade_date)).all()
        for day_index, bar in enumerate(bars):
            close = previous * math.exp(.001 * return_signal)
            bar.open, bar.close = previous, close
            bar.high, bar.low = max(previous, close) * 1.01, min(previous, close) * .99
            bar.amount = close * 1e6
            bar.turnover_rate = 10 + slope * .02 * (day_index + 1)
            previous = close
    db.commit()
    return path, days, symbol_ids, watchlist_id


def _register_two_basis(client):
    def version(code, formula):
        response = client.post(f'/api/v1/factors/{code}/versions', json={
            'formula_expr': formula, 'params': {}, 'direction': 'nonlinear',
            'postprocess': CONTRACT['postprocess'],
            'source_mapping': {'table': 'raw_daily_bars', 'field': 'turnover_rate'},
            'created_by': 'isolated_phase2',
        })
        assert response.status_code == 201, response.text
        return response.json()

    # Preserve the public version history and create the actual guarded v3.
    legacy = version(BASE, '(turnover_rate - mean_20d) / stddev_pop_20d')
    assert legacy['version'] == 2 and legacy['validation_status'] == 'invalid'
    base = version(BASE, CONTRACT['basis']['turnover_z20_v3']['formula_expr'])
    assert base['version'] == 3 and base['validation_status'] == 'valid'
    created = client.post('/api/v1/factors', json={
        'code': SQUARED, 'name': 'Isolated raw turnover Z20 square',
        'category': 'capital_flow', 'direction': 'nonlinear',
        'thesis': 'Isolated public two-basis contract; no market performance claim',
        'owner': 'isolated_phase2', 'default_missing_policy': 'exclude',
    })
    assert created.status_code == 201, created.text
    squared = version(SQUARED, CONTRACT['basis']['turnover_z20_squared']['formula_expr'])
    assert squared['version'] == 1 and squared['validation_status'] == 'valid'
    new_set = client.post('/api/v1/factor-sets', json={'name': 'Isolated quadratic basis'})
    assert new_set.status_code == 200, new_set.text
    set_id = new_set.json()['id']
    for code, definition in ((BASE, base), (SQUARED, squared)):
        member = client.post(f'/api/v1/factor-sets/{set_id}/members', json={
            'factor_code': code, 'factor_version': definition['version'],
            'role': 'feature', 'missing_policy': 'exclude',
        })
        assert member.status_code == 200, member.text
        assert member.json()['factor_version_id'] == definition['id']
    frozen = client.post(f'/api/v1/scoring/factorsets/{set_id}/freeze', json={
        'actor': 'isolated_phase2', 'reason': 'Review two immutable basis definitions',
    })
    assert frozen.status_code == 200, frozen.text
    readiness = client.get(f'/api/v1/scoring/factorsets/{set_id}/readiness')
    assert readiness.status_code == 200 and readiness.json()['ready'] is True
    return set_id, {BASE: base, SQUARED: squared}


def _train(client, days, set_id):
    submitted = client.post('/api/v1/scoring/tasks', json={
        'train_model': True, 'materialize_scores': False, 'factor_set_id': set_id,
        'start_date': days[0].isoformat(), 'end_date': days[-1].isoformat(),
        'window_days': 90, 'validation_days': 25,
        'factor_scope': {'factor_set_id': set_id},
        'hyperparameters': {'factor_set_id': set_id, 'fit_policy': 'cross_sectional_rank_v1'},
        'actor': 'isolated_phase2',
    })
    assert submitted.status_code == 200, submitted.text
    task = client.get(f"/api/v1/scoring/tasks/{submitted.json()['id']}").json()
    assert task['status'] == 'done', task
    result = task['result']
    assert result['trained_model_auto_activated'] is False
    # With materialize_scores=False the pipeline omits the consumer result.
    assert result.get('scoring_bridge', {}).get('materialized_count', 0) == 0
    assert result['batch_retention'] == {'status': 'SKIPPED', 'reason': 'TRAINING_ONLY_NON_PUBLISHING'}
    model_id = result['model']['model_run_id']
    response = client.get(f'/api/v1/factor-models/{model_id}')
    assert response.status_code == 200, response.text
    return task, response.json()


def test_public_two_basis_rank_training_snapshot_and_score_bridge_consume_both_coefficients(
    turnover_client, db_session, tmp_path,
):
    from app.models.score import Score
    from app.services.factors.store import FactorWarehouse
    from app.services.factors.training_input_snapshot import load_training_input_snapshot

    client = turnover_client
    path, days, symbol_ids, watchlist_id = _seed_quadratic_market(db_session, tmp_path, asymmetric=True)
    assert Path(path).resolve().is_relative_to(tmp_path.resolve())
    set_id, definitions = _register_two_basis(client)
    runtime_before = client.get('/api/v1/factor-models/runtime').json()
    task, model = _train(client, days, set_id)
    assert model['status'] == task['result']['model']['status'] == 'validated', model
    assert model['activated_at'] is None and model['production_eligible'] is True
    assert client.get('/api/v1/factor-models/runtime').json() == runtime_before
    assert db_session.scalar(select(func.count()).select_from(Score)) == 0
    expected_versions = {code: definition['version'] for code, definition in definitions.items()}
    assert model['feature_versions'] == expected_versions
    weights = {row['factor_code']: row for row in model['weights']}
    assert set(weights) == {BASE, SQUARED}
    assert all(abs(row['coefficient']) > 1e-5 for row in weights.values())
    assert {code: row['factor_version'] for code, row in weights.items()} == expected_versions
    hp, metrics = model['hyperparameters'], model['metrics']
    assert hp['fit_policy'] == 'cross_sectional_rank_v1'
    assert hp['protocol_version'] == 'ridge-temporal-cross-sectional-rank-v1'

    frame, manifest = load_training_input_snapshot(model['artifact_path'])
    assert Path(model['artifact_path']).resolve().is_relative_to(tmp_path.resolve())
    assert manifest['feature_versions'] == expected_versions
    assert list(frame.columns) == ['symbol', 'trade_date', BASE, SQUARED, 'target', 'exit_date']
    assert len(frame) == manifest['row_count'] == model['sample_count'] == metrics['total_sample_count']
    assert frame.symbol.nunique() == model['symbol_count']
    assert frame.trade_date.nunique() == model['trade_date_count'] == 90
    assert manifest['split_metadata']['fit_policy'] == hp['fit_policy']
    boundary = pd.Timestamp(model['validation_start_date']).date()
    training = frame.loc[(frame.trade_date < boundary) & (frame.exit_date < boundary)]
    validation = frame.loc[frame.trade_date >= boundary]
    assert len(training) == metrics['training_sample_count']
    assert len(validation) == metrics['validation_sample_count']
    assert training.exit_date.max() < boundary

    # Compare both snapshot columns to the exact public pipeline calculation
    # batch, rather than re-creating values or replacing the fit with a mock.
    warehouse = FactorWarehouse(path)
    with warehouse.connection(read_only=True) as conn:
        values = conn.execute('''SELECT symbol, trade_date, factor_code,
            factor_version, normalized_value FROM factor_values
            WHERE calc_batch_id=? AND factor_code IN (?, ?)''',
            [hp['factor_calc_batch_id'], BASE, SQUARED]).fetchdf()
    assert values.groupby('factor_code').factor_version.unique().map(list).to_dict() == {
        BASE: [3], SQUARED: [1]}
    values['trade_date'] = pd.to_datetime(values.trade_date).dt.date
    for code in (BASE, SQUARED):
        actual = frame[['symbol', 'trade_date', code]].merge(
            values.loc[values.factor_code.eq(code), ['symbol', 'trade_date', 'normalized_value']],
            on=['symbol', 'trade_date'], how='left', validate='one_to_one')
        assert len(actual) == len(frame) and actual.normalized_value.notna().all()
        np.testing.assert_array_equal(actual[code].to_numpy(), actual.normalized_value.to_numpy())

    public_detail = client.get(f"/api/v1/scoring/models/{model['id']}").json()
    assert public_detail['sample_count'] == model['sample_count']
    assert {(item['factor_code'], item['factor_version']) for item in public_detail['factors']} == set(expected_versions.items())
    relations = client.get(f"/api/v1/scoring/models/{model['id']}/relations").json()
    assert relations['factor_set_id'] == set_id
    assert {row['factor_code']: row['factor_version_id'] for row in relations['factors']} == {
        code: definition['id'] for code, definition in definitions.items()}

    # Explicit isolated approval happens after all preceding evidence reads.
    activated = client.post(f"/api/v1/scoring/models/{model['id']}/activate", json={
        'mode': 'ridge', 'actor': 'isolated_phase2',
        'note': 'Reviewed temporary two-feature model; this is not the running instance',
    })
    assert activated.status_code == 200, activated.text
    scoring = client.post('/api/v1/scoring/tasks', json={
        'train_model': False, 'materialize_scores': True,
        'start_date': days[-2].isoformat(), 'end_date': days[-1].isoformat(),
        'universe_source': 'watchlist', 'watchlist_id': watchlist_id,
        'hyperparameters': {'expected_model_run_id': model['id'], 'expected_weight_mode': 'ridge'},
        'actor': 'isolated_phase2',
    })
    assert scoring.status_code == 200, scoring.text
    scoring_task = client.get(f"/api/v1/scoring/tasks/{scoring.json()['id']}").json()
    assert scoring_task['status'] == 'done', scoring_task
    assert scoring_task['result']['completion_status'] == 'COMPLETED'
    latest = client.get(f'/api/v1/scores/latest/{symbol_ids[0]}')
    assert latest.status_code == 200, latest.text
    score = latest.json()
    assert score['factor_model_run_id'] == model['id'] and score['factor_set_id'] == set_id
    evidence = json.loads(score['factor_scores_json'])['_dynamic_model']
    contributions = {}
    for code in (BASE, SQUARED):
        entry = evidence['factors'][code]
        assert entry['coefficient'] == weights[code]['coefficient']
        assert entry['contribution'] == pytest.approx(entry['normalized_value'] * weights[code]['coefficient'])
        assert abs(entry['contribution']) > 1e-5
        contributions[code] = entry['contribution']
    assert evidence['model_alpha_raw'] == pytest.approx(sum(contributions.values()) + hp['final_intercept'])
    assert evidence['model_alpha_raw'] != pytest.approx(contributions[BASE] + hp['final_intercept'])
    assert db_session.scalar(select(func.count()).select_from(Score).where(Score.factor_model_run_id == model['id'])) == 2


def test_public_multi_basis_candidate_cannot_bypass_failed_original_member_pre_gate(
    turnover_client, db_session, tmp_path,
):
    from app.models.score import Score

    client = turnover_client
    _path, days, _symbols, _watchlist = _seed_quadratic_market(db_session, tmp_path, asymmetric=False)
    set_id, _definitions = _register_two_basis(client)
    before = client.get('/api/v1/factor-models/runtime').json()
    _task, model = _train(client, days, set_id)
    hp = model['hyperparameters']
    evidence = hp['training_gate_evidence']
    assert abs(evidence[BASE]['ic_mean']) < .01
    assert abs(evidence[SQUARED]['ic_mean']) > .5
    assert model['status'] == 'rejected'
    assert '[P2-G Pre] ic:' in model['rejection_reason'] and f'{BASE}:ic=' in model['rejection_reason']
    assert hp['selected_alpha'] is None and hp['final_intercept'] is None
    assert model['weights'] == [] and model['artifact_path'] is None
    assert model['metrics']['validation_ic'] is None
    assert model['production_eligible'] is False and model['activated_at'] is None
    refused = client.post(f"/api/v1/scoring/models/{model['id']}/activate", json={
        'mode': 'ridge', 'actor': 'isolated_phase2', 'note': 'Temporary negative admission check',
    })
    assert refused.status_code in (400, 409), refused.text
    assert client.get('/api/v1/factor-models/runtime').json() == before
    assert db_session.scalar(select(func.count()).select_from(Score)) == 0
