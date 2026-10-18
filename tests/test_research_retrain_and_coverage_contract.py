"""Research retraining preserves reviewed inputs; all stores/dispatch are isolated."""
from datetime import date
import json
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.whitebox


@pytest.fixture
def research_api(monkeypatch, db_session):
    from app.api.routes import scoring_facade as route
    from app.services.factors import __facade__ as facade
    from app.services import market_history_evidence as evidence
    from app.models.factor_model import FactorModelRun

    identity = {'dataset_id': 'trusted-original', 'manifest_sha256': 'a' * 64,
                'research_only': True}
    dataset = SimpleNamespace(identity=identity.copy(), dataset_id='trusted-original',
        manifest_sha256='a' * 64, symbols=('000001', '600000'), bars=[1, 2],
        calendar=(date(2024, 1, 2), date(2026, 1, 5)))
    hp = {'trusted_history_dataset': identity.copy(), 'factor_set_id': 'frozen-original',
          'factor_set_content_hash': 'fixed-hash', 'fit_policy': 'cross_sectional_rank_v1',
          'alphas': [.1, 1., 10., 100.]}
    model = FactorModelRun(id='original-model', model_type='ridge', asset_type='stock',
                           status='rejected', hyperparameters_json=json.dumps(hp))
    db_session.add(model); db_session.flush()
    def persist_hp():
        model.hyperparameters_json = json.dumps(hp)
        db_session.flush()
    ready = {'ready': True, 'factor_set_id': 'frozen-original',
             'details': {'content_hash': 'fixed-hash'}}
    monkeypatch.setattr(facade, '_open_session', lambda: (db_session, False))
    monkeypatch.setattr(facade, 'get_scoring_factor_set_readiness', lambda _: ready)
    monkeypatch.setattr(evidence, 'load_trusted_history_dataset', lambda _: dataset)
    submissions = []
    monkeypatch.setattr(route, 'create_scoring_task', lambda **kwargs:
        submissions.append(kwargs) or {'id': 'isolated-task', 'status': 'queued'})
    app = FastAPI()
    app.include_router(route.router, prefix='/api/v1')
    with TestClient(app) as client:
        yield client, hp, dataset, ready, submissions, persist_hp


def test_review_is_read_only_and_submission_preserves_original_inputs(research_api):
    client, hp, dataset, ready, submissions, _ = research_api
    url = '/api/v1/scoring/models/original-model/research-retrain'
    plan = client.get(url)
    assert plan.status_code == 200 and submissions == []
    assert plan.json()['independent_validation'] is False
    assert plan.json()['symbol_count'] == 2
    submitted = client.post(url, json={'request_id': '1' * 32, 'manifest_sha256': 'a' * 64, 'note': 'reviewed'})
    assert submitted.status_code == 200 and len(submissions) == 1
    payload = submissions[0]
    assert payload['trusted_history_dataset_id'] == dataset.dataset_id
    assert payload['factor_set_id'] == payload['hyperparameters']['factor_set_id'] == payload['factor_scope']['factor_set_id']
    assert payload['hyperparameters']['expected_trusted_history_manifest_sha256'] == 'a' * 64
    assert payload['hyperparameters']['source_model_id'] == 'original-model'
    assert payload['hyperparameters']['alphas'] == hp['alphas']
    assert payload['train_model'] is True and payload['materialize_scores'] is False
    assert payload['mirror_only'] is False and payload['universe_source'] is None
    assert payload['window_days'] == 250 and payload['validation_days'] == 50
    assert payload['start_date'] == '2024-01-02' and payload['end_date'] == payload['data_cutoff_date'] == '2026-01-05'


@pytest.mark.parametrize('problem', ['changed_manifest', 'changed_identity', 'unready_set', 'changed_set', 'ordinary', 'missing_policy'])
def test_changed_or_ineligible_research_inputs_never_dispatch(research_api, problem):
    client, hp, dataset, ready, submissions, persist_hp = research_api
    if problem == 'changed_identity': dataset.identity['manifest_sha256'] = 'b' * 64
    elif problem == 'unready_set': ready['ready'] = False
    elif problem == 'changed_set': ready['details']['content_hash'] = 'changed'
    elif problem == 'ordinary': hp.pop('trusted_history_dataset')
    elif problem == 'missing_policy': hp.pop('fit_policy')
    persist_hp()
    response = client.post('/api/v1/scoring/models/original-model/research-retrain', json={
        'request_id': '1' * 32, 'manifest_sha256': ('b' if problem == 'changed_manifest' else 'a') * 64})
    assert response.status_code == 409 and submissions == []


def test_cannot_inject_production_scoring_or_different_dataset(research_api):
    client, _, _, _, submissions, _ = research_api
    response = client.post('/api/v1/scoring/models/original-model/research-retrain', json={
        'request_id': '1' * 32, 'manifest_sha256': 'a' * 64, 'materialize_scores': True, 'trusted_history_dataset_id': 'other'})
    assert response.status_code == 422 and submissions == []


@pytest.mark.parametrize('freeze', [False, True])
def test_worker_rejects_reviewed_manifest_change_before_warehouse_creation(monkeypatch, freeze):
    from app.schemas.async_task import FactorPipelineCreate
    from app.services.factors.trusted_history_training import load_training_dataset
    from app.services import market_history_evidence as evidence
    days = tuple(pd.bdate_range('2024-01-02', periods=310).date)
    dataset = SimpleNamespace(calendar=days, manifest_sha256='b' * 64, identity={'research_only': True})
    monkeypatch.setattr(evidence, 'load_trusted_history_dataset', lambda _: dataset)
    payload = FactorPipelineCreate(trusted_history_dataset_id='trusted-original', train_model=True,
        materialize_scores=False, mirror_only=False, hyperparameters_json={
            'expected_trusted_history_manifest_sha256': 'a' * 64})
    with pytest.raises(ValueError, match='reviewed_manifest_changed'):
        load_training_dataset(payload, freeze=freeze)


def test_overview_reports_real_rows_and_separates_observation_only_mean(monkeypatch):
    from app.services.factors import __facade__ as facade, config, health, runtime, store
    from app.services.factors.definitions import FACTOR_DEFINITIONS
    rows = [health.FactorCoverage(factor_code=item.code, latest_trade_date='2026-10-14',
        universe_symbols=100, eligible_symbols=80 if item.model_enabled else 0,
        imputed_symbols=0, coverage=.8 if item.model_enabled else 0.) for item in FACTOR_DEFINITIONS]
    monkeypatch.setattr(facade, '_open_session', lambda: (object(), False))
    monkeypatch.setattr(config, 'get_factor_system_config', lambda _: SimpleNamespace(warehouse_path='isolated', feature_enabled=True))
    monkeypatch.setattr(runtime, 'get_factor_runtime_snapshot', lambda _: SimpleNamespace(
        score_weight_mode='manual', weight_mode='manual', active_model_run_id=None,
        version=11, updated_at=None, updated_by='test'))
    monkeypatch.setattr(store, 'FactorWarehouse', lambda _: object())
    monkeypatch.setattr(health, 'get_factor_health', lambda _: SimpleNamespace(factors=rows,
        reasons=['ep_ttm:stale'], warehouse_available=True, latest_bar_date='2026-10-16',
        calc_batch_id='frozen-default-batch', status='degraded'))
    overview = facade.get_score_runtime_overview()
    assert overview.active_factor_coverage_count == 8  # backward compatible aggregate
    assert overview.active_factor_avg_coverage == .5
    assert overview.model_factor_coverage_count == 5 and overview.model_factor_avg_coverage == .8
    assert overview.latest_factor_date == '2026-10-14'
    assert overview.latest_trade_date == '2026-10-16'
    assert overview.health_status == 'degraded' and overview.health_reasons == ['ep_ttm:stale']
    assert len(overview.factor_coverage) == 8
    assert sum(row['model_enabled'] for row in overview.factor_coverage) == 5
