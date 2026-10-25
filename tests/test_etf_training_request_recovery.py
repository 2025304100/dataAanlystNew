"""ETF request recovery at HTTP boundaries, with real temporary training stores."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_etf_history_training_contract import etf_dataset, frozen_etf_set
from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market

pytestmark = [pytest.mark.whitebox, pytest.mark.reap_workers]


@pytest.fixture
def etf_training_api(db_session, tmp_path, monkeypatch):
    from app.services.factors import pipeline_task
    monkeypatch.setattr(pipeline_task, "_run_factor_pipeline_process",
        lambda task_id, **kwargs: pipeline_task._run_factor_pipeline(task_id))
    from app.api.routes import factors, factor_sets, factor_models, scoring_facade
    from app.db.session import get_db
    application = FastAPI()
    for route in (factors.router, factor_sets.router, factor_models.router, scoring_facade.router):
        application.include_router(route, prefix='/api/v1')
    application.dependency_overrides[get_db] = lambda: db_session
    _seed_local_market(db_session, tmp_path, day_count=2)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def training_request(client, tmp_path, monkeypatch, *, symbols=24):
    factor_set_id = frozen_etf_set(client)
    dataset = etf_dataset(tmp_path, monkeypatch,
        symbols=tuple(str(510300 + index) for index in range(symbols)))
    return {'request_id': 'a' * 32, 'factor_set_id': factor_set_id, 'dataset_id': dataset.dataset_id,
            'manifest_sha256': dataset.manifest_sha256, 'note': '恢复同一次ETF训练'}


def wait_finished(client, task_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get('/api/v1/scoring/tasks/' + task_id)
        assert response.status_code == 200, response.text
        task = response.json()
        if task['status'] not in {'queued', 'running'}:
            return task
        time.sleep(.05)
    pytest.fail('ETF task did not finish')


def test_recover_and_repeat_a_completed_etf_request_reuses_the_same_task_and_model(
    etf_training_api, tmp_path, monkeypatch,
):
    client = etf_training_api
    body = training_request(client, tmp_path, monkeypatch)
    before = client.get('/api/v1/factor-models').json()
    submitted = client.post('/api/v1/scoring/etf-training', json=body)
    assert submitted.status_code == 200, submitted.text
    completed = wait_finished(client, submitted.json()['id'])
    assert completed['status'] == 'done', completed
    recovery = client.get('/api/v1/scoring/etf-training/requests/' + body['request_id'])
    assert recovery.status_code == 200, recovery.text
    assert recovery.json()['request'] == body
    assert recovery.json()['task']['id'] == completed['id']
    replay = client.post('/api/v1/scoring/etf-training', json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()['id'] == completed['id']
    assert replay.json()['result']['model']['model_run_id'] == completed['result']['model']['model_run_id']
    assert len(client.get('/api/v1/factor-models').json()['items']) == len(before['items']) + 1


@pytest.fixture
def stored_etf_request(db_session):
    """A saved submission whose original input files are no longer available."""
    from app.models.async_task import AsyncTaskRecord
    body = {'request_id': 'c' * 32, 'factor_set_id': 'archived-set', 'dataset_id': 'archived-etf',
            'manifest_sha256': 'b' * 64, 'note': '已提交的训练'}
    payload = {'factor_set_id': body['factor_set_id'], 'trusted_history_dataset_id': body['dataset_id'],
        'train_model': True, 'materialize_scores': False, 'mirror_only': False,
        'source_hint': 'factor_center:etf_train', 'window_days': 250, 'validation_days': 50,
        'factor_scope_json': {'factor_set_id': body['factor_set_id'], 'asset_type': 'etf'},
        'hyperparameters_json': {'factor_set_id': body['factor_set_id'], 'note': body['note'],
            'expected_trusted_history_manifest_sha256': body['manifest_sha256'],
            'etf_training_request_id': body['request_id']}}

    def save(status, *, invalid_context=False):
        if invalid_context:
            payload['materialize_scores'] = True
        db_session.add(AsyncTaskRecord(id='b887adf9c368b2aa36ca153ddc125b4e',
            task_type='factor_pipeline', status=status, stage=status, payload_json=json.dumps(payload),
            idempotency_key='request:31d19f32cb5caf627441a2b7ae8efd8167152b7bff43f1fa030c7f579f101250'))
        db_session.commit()
        return body
    return save


@pytest.mark.parametrize('status', ['queued', 'running', 'done', 'partial', 'failed', 'cancelled'])
def test_recovery_and_replay_preserve_saved_status_even_without_source_files(etf_training_api, stored_etf_request, status):
    body = stored_etf_request(status)
    client = etf_training_api
    before = client.get('/api/v1/factor-models').json()
    found = client.get('/api/v1/scoring/etf-training/requests/' + body['request_id'])
    assert found.status_code == 200, found.text
    assert found.json()['request'] == body
    assert found.json()['task']['status'] == status
    replay = client.post('/api/v1/scoring/etf-training', json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()['id'] == found.json()['task']['id']
    assert replay.json()['status'] == status
    assert client.get('/api/v1/factor-models').json() == before


@pytest.mark.parametrize('change', [
    {'factor_set_id': 'another-set'}, {'dataset_id': 'another-etf'},
    {'manifest_sha256': 'd' * 64}, {'note': '改过的备注'},
])
def test_same_request_id_cannot_change_the_training_inputs(etf_training_api, stored_etf_request, change):
    body = stored_etf_request('done')
    rejected = etf_training_api.post('/api/v1/scoring/etf-training', json={**body, **change})
    assert rejected.status_code == 409, rejected.text
    found = etf_training_api.get('/api/v1/scoring/etf-training/requests/' + body['request_id'])
    assert found.json()['request'] == body
    assert etf_training_api.get('/api/v1/factor-models').json()['items'] == []


def test_recovery_refuses_a_task_with_conflicting_context(etf_training_api, stored_etf_request):
    body = stored_etf_request('done', invalid_context=True)
    url = '/api/v1/scoring/etf-training'
    assert etf_training_api.get(url + '/requests/' + body['request_id']).status_code == 409
    assert etf_training_api.post(url, json=body).status_code == 409


def test_missing_request_is_reported_without_creating_a_task(etf_training_api):
    response = etf_training_api.get('/api/v1/scoring/etf-training/requests/' + 'f' * 32)
    assert response.status_code == 200, response.text
    assert response.json() == {'request_id': 'f' * 32, 'state': 'not_found', 'request': None, 'task': None}
    assert etf_training_api.get('/api/v1/scoring/tasks').json() == []


def test_training_request_keeps_the_existing_twenty_etf_gate(etf_training_api, tmp_path, monkeypatch):
    body = training_request(etf_training_api, tmp_path, monkeypatch, symbols=1)
    rejected = etf_training_api.post('/api/v1/scoring/etf-training', json=body)
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()['detail']['error_code'] == 'ETF_TRAINING_POOL_TOO_SMALL'
    assert etf_training_api.get('/api/v1/scoring/tasks').json() == []
    assert etf_training_api.get('/api/v1/factor-models').json()['items'] == []


def test_simultaneous_retries_create_only_one_etf_training_model(etf_training_api, tmp_path, monkeypatch):
    client = etf_training_api
    body = training_request(client, tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post('/api/v1/scoring/etf-training', json=body), range(4)))
    assert [item.status_code for item in responses] == [200] * 4, [item.text for item in responses]
    assert len({item.json()['id'] for item in responses}) == 1
    completed = wait_finished(client, responses[0].json()['id'])
    assert completed['status'] == 'done', completed
    assert len(client.get('/api/v1/factor-models').json()['items']) == 1
    assert len(client.get('/api/v1/scoring/tasks').json()) == 1
