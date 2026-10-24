"""Acceptance guards use isolated stores; normal business rules remain intact."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.services import inspection_acceptance_readonly as guard
from app.services import async_tasks
from app.models.app_setting import AppSetting

pytestmark = pytest.mark.whitebox


@pytest.fixture
def readonly(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///'+str(tmp_path/'readonly.db'), connect_args={'check_same_thread': False})
    with engine.begin() as c:
        c.execute(text('CREATE TABLE guard_probe (id INTEGER PRIMARY KEY, value TEXT)'))
        c.execute(text("INSERT INTO guard_probe VALUES (1, 'retained')"))
    AppSetting.__table__.create(engine)
    # Restore the disabled flag after every test; listeners are process-wide but gated.
    monkeypatch.setattr(guard, '_enabled', False)
    guard.enable_acceptance_read_only()
    yield engine
    engine.dispose()


@pytest.mark.parametrize('method,path', [
    ('POST','/api/v1/portfolios/1/inspection/reconciliation-job/recheck'),
    ('POST','/api/v1/portfolios/1/reconcile'),
    ('POST','/api/v1/portfolios/1/inspection/repairs'),
    ('POST','/api/v1/portfolios/1/inspection/repairs/task/retry'),
    ('POST','/api/v1/portfolios/1/inspection/repairs/task/cancel'),
    ('POST','/api/v1/portfolios/1/inspection/evidence-supplements'),
    ('POST','/api/v1/portfolios/1/inspection/material-investigations'),
    ('POST','/api/v1/portfolios/1/inspection/coverage/preview'),
    ('POST','/api/v1/portfolios/1/inspection/guide/review'),
    ('POST','/api/v1/portfolios/2/inspection/reconciliation-job/recheck/'),
    ('PUT','/unlisted'), ('PATCH','/unlisted'), ('DELETE','/unlisted'),
])
def test_all_mutations_are_rejected_before_handler(readonly, method, path):
    app=FastAPI();calls=[]
    @app.api_route('/{path:path}', methods=['GET','POST','PUT','PATCH','DELETE'])
    def downstream(path):
        calls.append(path)
        return {'unexpected':True}
    guard.install_acceptance_http_guard(app)
    with TestClient(app) as client:
        for _ in range(2):
            r=client.request(method,path,headers={'X-Acceptance-Read-Only':'false'},json={'request_key':'same-original-key'})
            assert r.status_code==423 and r.json()['request_registered'] is False and r.json()['task_dispatched'] is False
    assert calls==[]


@pytest.mark.parametrize('sql', [
    "INSERT INTO guard_probe VALUES (2, 'new')", "UPDATE guard_probe SET value='changed'", 'DELETE FROM guard_probe',
    'DROP TABLE guard_probe', 'CREATE TABLE unexpected (id INTEGER)',
    '/* ignored comment */ UPDATE guard_probe SET value=\'changed\'',
    "WITH ids AS (SELECT id FROM guard_probe) DELETE FROM guard_probe WHERE id IN (SELECT id FROM ids)",
    'PRAGMA user_version=77', 'SELECT 1; DELETE FROM guard_probe',
])
def test_sql_write_backstop_covers_core_and_comments(readonly, sql):
    with readonly.connect() as c:
        with pytest.raises(guard.AcceptanceReadOnlyError):c.execute(text(sql))
        assert c.scalar(text('SELECT value FROM guard_probe'))=='retained'


def test_get_side_effects_and_orm_flush_are_also_blocked(readonly):
    app=FastAPI()
    @app.get('/legacy-get-with-side-effect')
    def handler():
        with readonly.begin() as c:c.execute(text("UPDATE guard_probe SET value='changed'"))
    guard.install_acceptance_http_guard(app)
    with TestClient(app) as client:assert client.get('/legacy-get-with-side-effect').status_code==423
    with Session(readonly) as db:
        db.add(AppSetting(key='unexpected-write',value_json='{}'))
        with pytest.raises(guard.AcceptanceReadOnlyError):db.commit()
        db.rollback()


def test_new_control_plane_engine_cannot_bypass_guard(readonly):
    other=create_engine(readonly.url)
    try:
        with other.begin() as c:
            assert c.scalar(text('SELECT COUNT(*) FROM guard_probe'))==1
            with pytest.raises(guard.AcceptanceReadOnlyError):c.execute(text('DELETE FROM guard_probe'))
    finally:other.dispose()


def test_task_registration_and_worker_start_fail_before_db_or_thread(readonly, monkeypatch):
    factory=__import__('unittest.mock',fromlist=['Mock']).Mock(side_effect=AssertionError('must not open DB'))
    monkeypatch.setattr(async_tasks,'_pick_session_factory',factory)
    monkeypatch.setattr(async_tasks,'_current_db_url',factory)
    for call in [lambda:async_tasks.create_async_task('probe',{}),
        lambda:async_tasks.create_async_task_once('probe',{},task_id='a'*32,request_fingerprint='b'*64),
        lambda:async_tasks._start_worker('retained-task',factory)]:
        with pytest.raises(guard.AcceptanceReadOnlyError):call()
    factory.assert_not_called()


def test_read_queries_and_non_acceptance_process_behavior(readonly, monkeypatch):
    with readonly.connect() as c:
        assert c.scalar(text('/* read only */ SELECT value FROM guard_probe'))=='retained'
        assert c.execute(text('PRAGMA table_info(guard_probe)')).all()
    monkeypatch.setattr(guard,'_enabled',False)
    with readonly.begin() as c:c.execute(text("UPDATE guard_probe SET value='normal-mode'"))
    guard.require_task_writes_allowed()


def test_read_methods_remain_available_after_new_app_instances(readonly):
    for _ in range(2):
        app=FastAPI()
        @app.api_route('/read',methods=['GET','HEAD','OPTIONS'])
        def handler():return {'read_only':True}
        guard.install_acceptance_http_guard(app)
        with TestClient(app) as client:
            for method in ['GET','HEAD','OPTIONS']:assert client.request(method,'/read').status_code==200
            assert client.post('/read').status_code==423


def test_status_read_does_not_depend_on_routes_after_the_frontend_mount(readonly):
    app=FastAPI()
    @app.get('/{path:path}')
    def frontend(path):return {'frontend':True}
    guard.install_acceptance_http_guard(app,status_provider=lambda:{'read_only':True})
    with TestClient(app) as client:
        assert client.get('/api/v1/inspection-acceptance/status').json()=={'read_only':True}
        assert client.post('/api/v1/inspection-acceptance/status').status_code==423


def test_mode_metadata_is_explicit_and_does_not_change_business_permissions(readonly, monkeypatch):
    assert guard.acceptance_execution_status()['read_only'] is True
    assert guard.acceptance_execution_status()['task_registration_allowed'] is False
    monkeypatch.setattr(guard,'_enabled',False)
    assert guard.acceptance_execution_status()=={'mode':'application','read_only':False}
