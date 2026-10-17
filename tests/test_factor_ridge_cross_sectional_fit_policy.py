"""Opt-in rank-target Ridge through training and public model reads."""
from __future__ import annotations

from datetime import timedelta
import json

import numpy as np
import pandas as pd
import pytest

from tests.test_factor_ridge_temporal_holdout_v2 import temporal_inputs, train, read

pytestmark = pytest.mark.whitebox


def test_opt_in_rank_fit_has_distinct_identity_and_reproducible_public_evidence(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    raw = train(db_session, warehouse, start)
    raw_before = read(client, raw.model_run_id)
    ranked = train(db_session, warehouse, start,
        hyperparameters={'fit_policy': 'cross_sectional_rank_v1'})
    model = read(client, ranked.model_run_id)
    assert ranked.model_run_id != raw.model_run_id
    assert model['hyperparameters']['protocol_version'] == 'ridge-temporal-cross-sectional-rank-v1'
    assert model['hyperparameters']['fit_policy'] == 'cross_sectional_rank_v1'
    # Eight strictly ordered outcomes each day yield labels (-3.5..3.5)/9.
    # The fixture feature spacing is half-units, so this slope is near 2/9,
    # rather than the raw-return fit's approximately .025 slope.
    assert .21 < model['weights'][0]['coefficient'] < .23
    assert model['metrics']['validation_ic_method'] == 'pooled_spearman'
    assert model['activated_at'] is None
    assert read(client, raw.model_run_id) == raw_before
    again = train(db_session, warehouse, start,
        hyperparameters={'fit_policy': 'cross_sectional_rank_v1'})
    assert again.reused and again.model_run_id == ranked.model_run_id


def test_rank_fit_ignores_future_labels_and_reports_metrics_against_original_returns(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    first = train(db_session, warehouse, start, fit_policy='cross_sectional_rank_v1')
    before = read(client, first.model_run_id)
    with warehouse.connection() as conn:
        conn.execute("""UPDATE factor_targets SET target_value=-50*target_value
            WHERE calc_batch_id='temporal-targets' AND signal_date>=?""",
            [start+timedelta(days=65)])
    changed = train(db_session, warehouse, start,
        hyperparameters={'fit_policy': 'cross_sectional_rank_v1'})
    after = read(client, changed.model_run_id)
    assert first.model_run_id != changed.model_run_id
    assert [w['coefficient'] for w in before['weights']] == [w['coefficient'] for w in after['weights']]
    assert before['hyperparameters']['selected_alpha'] == after['hyperparameters']['selected_alpha']
    assert before['hyperparameters']['final_intercept'] == after['hyperparameters']['final_intercept']
    assert before['metrics']['validation_ic'] > 0 > after['metrics']['validation_ic']
    assert changed.status == 'rejected' and after['activated_at'] is None
    from app.services.factors.training_input_snapshot import load_training_input_snapshot
    frame, manifest = load_training_input_snapshot(after['artifact_path'])
    validation = frame.loc[frame.trade_date >= start+timedelta(days=65)]
    prediction = (validation.holdout_feature.to_numpy()*after['weights'][0]['coefficient']
                  + after['hyperparameters']['final_intercept'])
    # Compare reported acceptance MSE with the original return labels, not
    # transformed rank labels. Rank-target MSE would be orders smaller here.
    assert after['metrics']['validation_mse'] == pytest.approx(float(np.mean((prediction-validation.target.to_numpy())**2)), abs=1e-14)
    assert after['metrics']['validation_mse_target'] == 'original_untransformed_target_5d_return'
    assert manifest['protocol_version'] == after['hyperparameters']['protocol_version']
    assert manifest['split_metadata']['fit_policy'] == 'cross_sectional_rank_v1'
    assert manifest['split_metadata']['fit_target_policy'] == after['hyperparameters']['fit_target_policy']
    assert read(client, first.model_run_id) == before


@pytest.mark.parametrize('bad_policy', ['unknown', '', None, True, 1, {'target': 'rank'}, ['cross_sectional_rank_v1']])
def test_unknown_json_fit_policy_is_rejected_before_silent_raw_model_reuse(db_session, temporal_inputs, bad_policy):
    warehouse, start, client = temporal_inputs
    before = client.get('/api/v1/factor-models').json()
    with pytest.raises(ValueError, match='unsupported_fit_policy'):
        train(db_session, warehouse, start, hyperparameters={'fit_policy': bad_policy})
    assert client.get('/api/v1/factor-models').json() == before


def test_policy_conflicts_and_explicit_model_id_cannot_alias_other_protocol(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    raw = train(db_session, warehouse, start)
    before = read(client, raw.model_run_id)
    assert 'fit_policy' not in before['hyperparameters']
    assert 'fit_target_policy' not in before['hyperparameters']
    same = train(db_session, warehouse, start, fit_policy='raw_return_v2')
    assert same.reused and same.model_run_id == raw.model_run_id
    with pytest.raises(ValueError, match='fit_policy_conflict'):
        train(db_session, warehouse, start, fit_policy='raw_return_v2',
            hyperparameters={'fit_policy': 'cross_sectional_rank_v1'})
    with pytest.raises(ValueError, match='model_run_protocol_conflict'):
        train(db_session, warehouse, start, fit_policy='cross_sectional_rank_v1', model_run_id=raw.model_run_id)
    assert read(client, raw.model_run_id) == before


def test_public_fit_target_has_average_ties_neutral_constants_and_equal_date_weights():
    from app.services.factors.fit_policy import prepare_fit_target
    frame = pd.DataFrame({'trade_date': ['day1']*4+['day2']*2+['day3'],
        'target': [0., 10., 10., 30., -20., 20., 999.],
        'feature': [8., 7., 6., 5., 4., 3., 2.]})
    original = frame.copy(deep=True)
    prepared = prepare_fit_target(frame, 'cross_sectional_rank_v1')
    assert prepared.target.tolist() == pytest.approx([-.3, 0., 0., .3, -1/6, 1/6, 0.])
    assert prepared.sample_weight.tolist() == pytest.approx([7/12]*4+[7/6]*2+[7/3])
    pd.testing.assert_frame_equal(frame, original)


def test_rank_candidate_still_scores_unchanged_normalized_features_and_reads_scores_over_http(db_session, temporal_inputs):
    from app.api.routes import scores
    from app.models.factor_runtime import FactorRuntimeState
    from app.models.factor_evaluation import FactorSet
    from app.models.symbol import Symbol
    from app.services.factors.scoring_bridge import materialize_factor_scores

    warehouse, start, client = temporal_inputs
    # The shared temporal-only fixture deliberately has a short hash. This
    # consumer fixture also needs a well-formed frozen-set signature.
    db_session.get(FactorSet, 'temporal-set').content_hash = 'a'*64
    db_session.commit()
    result = train(db_session, warehouse, start, fit_policy='cross_sectional_rank_v1')
    model = read(client, result.model_run_id)
    assert result.status == 'validated'
    symbols = [Symbol(symbol=f'{i+1:06d}', name=f'Isolated stock {i}',
        asset_type='stock', market='CN', is_active=1) for i in range(8)]
    db_session.add_all(symbols)
    # Fixture-only runtime for public Score reads. No activation endpoint or
    # live database is used by this isolated consumer compatibility test.
    db_session.add(FactorRuntimeState(id=1, weight_mode='ridge',
        active_model_run_id=result.model_run_id, updated_by='isolated_consumer_fixture'))
    db_session.flush()
    ready = read(client, result.model_run_id)
    assert ready['production_eligible'] is True, json.dumps(ready, ensure_ascii=False, indent=2)
    materialized = materialize_factor_scores(db_session, warehouse,
        trade_date=start+timedelta(days=79), factor_calc_batch_id='temporal-factors',
        model_run_id=result.model_run_id, mode='ridge')
    assert materialized.materialized_count == 8
    client.app.include_router(scores.router, prefix='/api/v1')
    first = client.get(f'/api/v1/scores/latest/{symbols[0].id}')
    last = client.get(f'/api/v1/scores/latest/{symbols[-1].id}')
    assert first.status_code == last.status_code == 200, (first.text, last.text)
    low, high = first.json(), last.json()
    assert low['factor_model_run_id'] == high['factor_model_run_id'] == result.model_run_id
    assert low['model_alpha_score'] == 0. and high['model_alpha_score'] == 100.
    explanation = json.loads(high['factor_scores_json'])['_dynamic_model']
    actual = explanation['factors']['holdout_feature']
    assert actual['normalized_value'] == pytest.approx(1.75+.003*79)
    assert actual['coefficient'] == model['weights'][0]['coefficient']
    assert actual['contribution'] == pytest.approx((1.75+.003*79)*actual['coefficient'])
    assert explanation['model_alpha_raw'] == pytest.approx(actual['contribution']+model['hyperparameters']['final_intercept'])
    assert read(client, result.model_run_id)['activated_at'] is None


def test_saved_rank_coefficients_replay_date_balanced_objective_and_raw_acceptance(db_session, temporal_inputs):
    from app.services.factors.training_input_snapshot import load_training_input_snapshot

    warehouse, start, client = temporal_inputs
    # Unequal cross-sections expose accidental row-weighted fitting: small
    # dates have only half the names but must retain the same total weight.
    with warehouse.connection() as conn:
        conn.execute("""DELETE FROM factor_values WHERE calc_batch_id='temporal-factors'
            AND trade_date<? AND symbol>'000004'""", [start+timedelta(days=40)])
    fitted = train(db_session, warehouse, start, fit_policy='cross_sectional_rank_v1')
    model = read(client, fitted.model_run_id)
    hp, metrics = model['hyperparameters'], model['metrics']
    frame, _ = load_training_input_snapshot(model['artifact_path'])
    v0 = pd.Timestamp(model['validation_start_date']).date()
    training = frame.loc[(frame.trade_date < v0) & (frame.exit_date < v0)]
    # Independent weighted one-feature normal equations, using rank literals
    # implied by this strictly increasing four-/eight-name fixture.
    weight, label = [], []
    for _, group in training.groupby('trade_date'):
        n = len(group)
        label.extend((np.arange(1, n+1)/(n+1)-.5).tolist())
        weight.extend([len(training)/(training.trade_date.nunique()*n)]*n)
    x = training.holdout_feature.to_numpy()
    y, w = np.asarray(label), np.asarray(weight)
    x0, y0 = np.average(x, weights=w), np.average(y, weights=w)
    coefficient = np.sum(w*(x-x0)*(y-y0))/(np.sum(w*(x-x0)**2)+hp['selected_alpha'])
    intercept = y0-coefficient*x0
    persisted = float(format(float(np.float32(coefficient)), '.6g'))
    assert model['weights'][0]['coefficient'] == persisted
    assert hp['final_intercept'] == pytest.approx(intercept, abs=1e-13)
    holdout = frame.loc[frame.trade_date >= v0]
    prediction = pd.Series(holdout.holdout_feature.to_numpy()*persisted+intercept)
    raw_target = holdout.target.reset_index(drop=True)
    assert metrics['validation_ic'] == pytest.approx(prediction.corr(raw_target, method='spearman'), abs=1e-14)
    assert metrics['validation_mse'] == pytest.approx(float(np.mean((prediction-raw_target)**2)), abs=1e-14)
    assert hp['fit_target_policy']['sample_weight'] == 'n_rows / (n_dates * date_row_count)'
