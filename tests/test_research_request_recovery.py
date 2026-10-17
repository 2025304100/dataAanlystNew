"""Durable request recovery against real isolated SQL storage; no fitting or network."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.test_research_retrain_and_coverage_contract import research_api


@pytest.fixture
def request_store(tmp_path, monkeypatch):
    from app.models.async_task import AsyncTaskRecord
    from app.services import async_tasks
    engine = create_engine(f'sqlite:///{tmp_path / "requests.db"}', connect_args={'check_same_thread': False})
    AsyncTaskRecord.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(async_tasks, '_pick_session_factory', lambda _: factory)
    yield factory
    engine.dispose()


@pytest.mark.parametrize('status', ['queued', 'running', 'done', 'failed', 'cancelled'])
def test_same_request_returns_original_terminal_or_running_task(request_store, status):
    from app.models.async_task import AsyncTaskRecord
    from app.services.async_tasks import create_async_task_once
    first, created = create_async_task_once('factor_pipeline', {'original': True}, task_id='1' * 32, request_fingerprint='a' * 64)
    assert created
    with request_store() as db:
        db.get(AsyncTaskRecord, first.id).status = status
        db.commit()
    replay, created = create_async_task_once('factor_pipeline', {'original': True}, task_id=first.id, request_fingerprint='a' * 64)
    assert not created and replay.id == first.id and replay.status == status
    with pytest.raises(ValueError, match='conflict'):
        create_async_task_once('factor_pipeline', {}, task_id=first.id, request_fingerprint='b' * 64)


def test_concurrent_callers_have_one_persisted_task_and_one_dispatch_winner(request_store):
    from app.models.async_task import AsyncTaskRecord
    from app.services.async_tasks import create_async_task_once
    def submit(_):
        return create_async_task_once('factor_pipeline', {}, task_id='2' * 32, request_fingerprint='c' * 64)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(12)))
    assert sum(created for _, created in results) == 1
    assert {task.id for task, _ in results} == {'2' * 32}
    with request_store() as db:
        assert len(db.scalars(select(AsyncTaskRecord)).all()) == 1


@pytest.mark.parametrize('status', ['done', 'failed', 'cancelled'])
def test_http_recovers_original_task_without_rechecking_sources_or_dispatching(research_api, db_session, monkeypatch, status):
    from app.api.routes import scoring_facade as route
    from app.models.async_task import AsyncTaskRecord
    from app.services.factors.research_requests import research_request_identity
    client, _, _, _, submissions, _ = research_api
    model, request_id, manifest, note = 'original-model', '3' * 32, 'a' * 64, 'reviewed'
    task_id, fingerprint = research_request_identity(model, request_id, manifest, note)
    payload = {'train_model': True, 'materialize_scores': False, 'mirror_only': False,
        'source_hint': 'factor_center:research_retrain', 'trusted_history_dataset_id': 'trusted-original',
        'hyperparameters_json': {'source_model_id': model, 'research_retrain_request_id': request_id,
            'expected_trusted_history_manifest_sha256': manifest, 'note': note}}
    db_session.add(AsyncTaskRecord(id=task_id, task_type='factor_pipeline', status=status,
        stage=status, percent=100, payload_json=json.dumps(payload), idempotency_key='request:' + fingerprint))
    db_session.flush()
    monkeypatch.setattr(route, 'get_research_retrain_plan', lambda _: pytest.fail('recovery must not reopen dataset'))
    url = f'/api/v1/scoring/models/{model}/research-retrain'
    found = client.get(url + '/requests/' + request_id)
    assert found.status_code == 200, found.text
    assert found.json()['task']['id'] == task_id and found.json()['task']['status'] == status
    assert found.json()['manifest_sha256'] == manifest and found.json()['note'] == note
    repeated = client.post(url, json={'request_id': request_id, 'manifest_sha256': manifest, 'note': note})
    assert repeated.status_code == 200 and repeated.json()['id'] == task_id and submissions == []
    conflict = client.post(url, json={'request_id': request_id, 'manifest_sha256': manifest, 'note': 'changed'})
    assert conflict.status_code == 409 and submissions == []
    assert client.get(url.replace(model, 'different-model') + '/requests/' + request_id).json()['state'] == 'not_found'


@pytest.mark.parametrize('request_id', [None, 'short', 'A' * 32, '../escape'])
def test_request_id_required_before_any_dispatch(research_api, request_id):
    client, _, _, _, submissions, _ = research_api
    body = {'manifest_sha256': 'a' * 64}
    if request_id is not None:
        body['request_id'] = request_id
    assert client.post('/api/v1/scoring/models/original-model/research-retrain', json=body).status_code == 422
    assert submissions == []


def test_dispatch_replay_never_starts_worker_again(request_store, monkeypatch):
    from app.services.factors import pipeline_task as pipeline
    from app.services.factors.research_requests import research_request_identity
    from app.schemas.async_task import FactorPipelineCreate
    from app.services.async_tasks import create_async_task_once
    monkeypatch.setattr(pipeline, 'get_session_local', lambda: request_store)
    monkeypatch.setattr(pipeline, '_start_worker', lambda *args: pytest.fail('duplicate dispatch'))
    monkeypatch.setattr(pipeline, 'get_current_factor_system_config', lambda: pytest.fail('no source revalidation on replay'))
    payload = FactorPipelineCreate(trusted_history_dataset_id='trusted-original', train_model=True,
        materialize_scores=False, source_hint='factor_center:research_retrain', hyperparameters_json={
            'research_retrain_request_id': '4' * 32, 'source_model_id': 'model',
            'expected_trusted_history_manifest_sha256': 'a' * 64, 'note': ''})
    task_id, fingerprint = research_request_identity('model', '4' * 32, 'a' * 64, '')
    create_async_task_once('factor_pipeline', payload.model_dump(), task_id=task_id, request_fingerprint=fingerprint)
    replay = pipeline.create_factor_pipeline_task(payload)
    assert replay['id'] == task_id


def test_first_dispatch_wins_once_and_following_request_reuses_it(request_store, monkeypatch):
    from types import SimpleNamespace
    from app.services.factors import pipeline_task as pipeline, trusted_history_training as training
    from app.schemas.async_task import FactorPipelineCreate
    monkeypatch.setattr(pipeline, 'get_session_local', lambda: request_store)
    monkeypatch.setattr(pipeline, 'get_current_factor_system_config', lambda: SimpleNamespace(feature_enabled=True))
    monkeypatch.setattr(pipeline, '_require_ready_training_factor_set', lambda _: None)
    monkeypatch.setattr(training, 'load_training_dataset', lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, 'freeze_score_universe', lambda *args: None)
    monkeypatch.setattr(pipeline, '_validate_requested_runtime', lambda *args: None)
    monkeypatch.setattr(pipeline, '_pin_scoring_runtime', lambda *args: None)
    monkeypatch.setattr(pipeline, 'list_async_tasks', lambda **kwargs: [])
    dispatched = []
    monkeypatch.setattr(pipeline, '_start_worker', lambda task_id, worker: dispatched.append(task_id))
    payload = FactorPipelineCreate(trusted_history_dataset_id='trusted-original', train_model=True,
        materialize_scores=False, source_hint='factor_center:research_retrain', hyperparameters_json={
            'research_retrain_request_id': '5' * 32, 'source_model_id': 'model',
            'expected_trusted_history_manifest_sha256': 'a' * 64, 'note': ''})
    first = pipeline.create_factor_pipeline_task(payload)
    second = pipeline.create_factor_pipeline_task(payload)
    assert first['id'] == second['id'] and dispatched == [first['id']]


def test_database_unique_key_recovers_a_winner_that_appears_after_initial_lookup(request_store, monkeypatch):
    from app.models.async_task import AsyncTaskRecord
    from app.services import async_tasks
    first, _ = async_tasks.create_async_task_once('factor_pipeline', {'original': True},
        task_id='6' * 32, request_fingerprint='a' * 64)
    session = request_store()
    original_get, lookups = session.get, []
    def miss_first(model, key):
        lookups.append(key)
        return None if len(lookups) == 1 else original_get(model, key)
    monkeypatch.setattr(session, 'get', miss_first)
    monkeypatch.setattr(async_tasks, '_pick_session_factory', lambda _: lambda: session)
    replay, created = async_tasks.create_async_task_once('factor_pipeline', {'original': True},
        task_id=first.id, request_fingerprint='a' * 64)
    assert not created and replay.id == first.id and len(lookups) == 2


def test_http_submit_persists_receipt_then_timeout_recovery_and_replay_share_one_task(research_api, db_session, monkeypatch):
    from app.api.routes import scoring_facade as route
    from app.services.factors.research_requests import research_request_identity
    from app.models.async_task import AsyncTaskRecord
    client, _, _, _, submissions, _ = research_api
    def dispatch(**kwargs):
        submissions.append(kwargs)
        hp = kwargs['hyperparameters']
        task_id, fingerprint = research_request_identity(hp['source_model_id'],
            hp['research_retrain_request_id'], hp['expected_trusted_history_manifest_sha256'], hp['note'])
        payload = {'hyperparameters_json': hp, 'trusted_history_dataset_id': kwargs['trusted_history_dataset_id'],
            'train_model': kwargs['train_model'], 'materialize_scores': kwargs['materialize_scores'],
            'mirror_only': kwargs['mirror_only'], 'source_hint': kwargs['source_hint']}
        db_session.add(AsyncTaskRecord(id=task_id, task_type='factor_pipeline', status='queued', stage='queued',
            percent=0, payload_json=json.dumps(payload), idempotency_key='request:' + fingerprint))
        db_session.flush()
        return {'id': task_id, 'status': 'queued'}
    monkeypatch.setattr(route, 'create_scoring_task', dispatch)
    body = {'request_id': '7' * 32, 'manifest_sha256': 'a' * 64, 'note': 'reviewed'}
    url = '/api/v1/scoring/models/original-model/research-retrain'
    # Simulate a client that did not receive/use the first response after commit.
    original = client.post(url, json=body)
    assert original.status_code == 200
    recovery = client.get(url + '/requests/' + body['request_id'])
    replay = client.post(url, json=body)
    assert recovery.json()['task']['id'] == replay.json()['id'] == original.json()['id']
    assert len(submissions) == 1
    task = db_session.get(AsyncTaskRecord, original.json()['id'])
    payload = json.loads(task.payload_json)
    assert payload['materialize_scores'] is False and payload['trusted_history_dataset_id'] == 'trusted-original'
    payload['materialize_scores'] = True
    task.payload_json = json.dumps(payload); db_session.flush()
    assert client.get(url + '/requests/' + body['request_id']).status_code == 409
