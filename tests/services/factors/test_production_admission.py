"""Real bundle/file/HTTP checks; no production training or activation."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import event

from app.services.factors import production_admission as admission
from app.services.factors.store import FactorWarehouse
from tests.services.factors.test_input_replay import source_bundle

pytestmark = pytest.mark.whitebox


@pytest.fixture
def prepared(source_bundle, monkeypatch, tmp_path):
    _, _, days = source_bundle
    warehouse = FactorWarehouse(tmp_path / 'warehouse.duckdb')
    warehouse.initialize()
    monkeypatch.setattr(admission, 'PLAN_ROOT', tmp_path / 'plans')
    monkeypatch.setattr(admission, 'get_factor_system_config', lambda _: SimpleNamespace(warehouse_path=warehouse.path))
    parent = dict(model_id='research-parent', factor_set_id='frozen-set', factor_set_content_hash='a'*64,
        feature_versions={'turnover_z20': 3}, coefficients={'turnover_z20': -.2}, intercept=.1,
        input_fingerprint='b'*64, snapshot_manifest_sha256='c'*64, snapshot_samples_sha256='d'*64,
        training_cohort=[f'{i+1:06d}' for i in range(23)], fit_policy='cross_sectional_rank_v1',
        alphas=[.1, 1., 10.], model_gate={'minimum_validation_ic': 0.}, governance_gate_config={},
        research_metric_gate='validated', research_validation_ic=.05, observed_through=str(days[-1]))
    release = dict(ready=False, code='MODEL_RESEARCH_DATASET_ONLY', message='Research only')
    monkeypatch.setattr(admission, '_parent_context', lambda db, model_id: (deepcopy(parent), release))
    return parent, warehouse, days


def inspect(db):
    return admission.inspect_production_admission(db, 'research-parent', bundle_id='test-inputs')


def freeze(db, report):
    return admission.freeze_production_admission(db, 'research-parent', bundle_id='test-inputs',
        expected_identity_sha256=report['identity_sha256'])


def test_real_read_does_not_create_plan_or_write_sql(prepared, db_session):
    sql = []
    def capture(conn, cursor, statement, *args): sql.append(statement)
    event.listen(db_session.get_bind(), 'before_cursor_execute', capture)
    try:
        result = inspect(db_session)
        assert result['planning_only'] and result['read_only']
        assert result['bundle']['raw_trade_dates'] == 28
        assert result['potential_mature_signal_days_upper_bound'] == 4
        assert result['cohort_change'] == dict(parent_count=23, planned_count=24, added=['000024'], removed=[])
        gates = {g['code']: g for g in result['gates']}
        assert gates['PRODUCTION_SAMPLE_SPAN']['actual'] == 28
        assert gates['PRODUCTION_SAMPLE_SPAN']['required'] == 274
        assert gates['UNSEEN_MATURE_HOLDOUT']['actual'] == 0
        assert gates['POINT_IN_TIME_SOURCE_PROOF']['actual'] == 0
        assert gates['INDEPENDENT_NET_COST_EVIDENCE']['passed'] is None
        assert not result['sample_precheck_ready'] and not result['production_eligible']
        assert not admission.PLAN_ROOT.exists()
        frozen = freeze(db_session, result)
        again = freeze(db_session, frozen)
        assert again['frozen_plan'] == frozen['frozen_plan']
        assert len(list(admission.PLAN_ROOT.iterdir())) == 1
        assert not frozen['frozen_plan']['is_training_or_release_certificate']
        assert frozen['training_not_submitted'] and frozen['model_not_activated']
        assert not any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER')) for s in sql)
    finally:
        event.remove(db_session.get_bind(), 'before_cursor_execute', capture)


def test_stale_review_does_not_freeze(prepared, db_session):
    parent, _, _ = prepared
    reviewed = inspect(db_session)
    parent['coefficients']['turnover_z20'] = .3
    with pytest.raises(ValueError, match='review_changed'): freeze(db_session, reviewed)
    assert not admission.PLAN_ROOT.exists()


def test_failed_research_cannot_freeze(prepared, db_session):
    prepared[0]['research_metric_gate'] = 'rejected'
    report = inspect(db_session)
    assert not report['can_freeze']
    with pytest.raises(ValueError, match='research_gate_not_passed'): freeze(db_session, report)
    assert not admission.PLAN_ROOT.exists()


def test_frozen_parent_mutation_is_rejected(prepared, db_session):
    freeze(db_session, inspect(db_session))
    prepared[0]['coefficients']['turnover_z20'] = .3
    with pytest.raises(ValueError, match='frozen_parent_changed'): inspect(db_session)
    assert len(list(admission.PLAN_ROOT.glob('*.json'))) == 1


def test_later_registered_bundle_keeps_original_boundary_and_cohort(prepared, db_session, monkeypatch):
    original = freeze(db_session, inspect(db_session))
    context, bars, days = admission._bundle_context('test-inputs')
    later_days = tuple(pd.bdate_range(date.fromisoformat(original['independent_after']), periods=90).date)
    later_bars = pd.DataFrame([dict(symbol=s, trade_date=pd.Timestamp(d)) for s in context['symbols'] for d in later_days])
    extended = {**context, 'bundle_id': 'later-inputs', 'bundle_manifest_sha256': 'f'*64,
        'dataset_manifest_sha256': 'e'*64, 'first_date': str(later_days[0]), 'last_date': str(later_days[-1])}
    monkeypatch.setattr(admission, '_bundle_context', lambda key: (extended, later_bars, later_days) if key=='later-inputs' else (context, bars, days))
    monkeypatch.setattr(admission, '_timely_dates', lambda *args: [])
    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None): return datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(admission, 'datetime', LaterClock)
    result = admission.inspect_production_admission(db_session, 'research-parent', bundle_id='later-inputs')
    assert result['frozen_plan'] == original['frozen_plan']
    assert result['identity_sha256'] == original['identity_sha256']
    assert result['independent_after'] == original['independent_after']
    assert result['independent_signal_days_upper_bound'] == 66
    assert result['gates'][4]['actual'] == 0  # A new bundle alone is not timely evidence.
    extended['symbols'] = extended['symbols'][:-1]
    with pytest.raises(ValueError, match='frozen_cohort_changed'):
        admission.inspect_production_admission(db_session, 'research-parent', bundle_id='later-inputs')


@pytest.mark.parametrize('tamper', ['checksum', 'filename'])
def test_frozen_file_integrity(prepared, db_session, tamper):
    result = freeze(db_session, inspect(db_session))
    path = admission.PLAN_ROOT / (result['frozen_plan']['plan_id']+'.json')
    if tamper == 'checksum':
        data = json.loads(path.read_text()); data['plan']['identity']['observed_through'] = '2000-01-01'
        path.write_text(json.dumps(data))
    else:
        path.rename(path.with_name('a'*64+'.json'))
    with pytest.raises(ValueError, match='frozen_plan_corrupt'): inspect(db_session)


def test_source_mutation_rejected_after_freeze(prepared, db_session, monkeypatch):
    freeze(db_session, inspect(db_session))
    context, bars, days = admission._bundle_context('test-inputs')
    context['bundle_manifest_sha256'] = 'e'*64
    monkeypatch.setattr(admission, '_bundle_context', lambda _: (context, bars, days))
    with pytest.raises(ValueError, match='frozen_source_changed'): inspect(db_session)


def test_current_late_receipts_cannot_certify_historical_dates(prepared):
    bundle, bars, days = admission._bundle_context('test-inputs')
    bundle['source_historical_asof_certified'] = True  # A caller flag has no authority.
    assert admission._timely_dates(bundle, bars, days, list(days[19:-5])) == []


def test_missing_warehouse_read_does_not_create_a_file(prepared, db_session, monkeypatch, tmp_path):
    path = tmp_path/'absent.duckdb'
    monkeypatch.setattr(admission, 'get_factor_system_config', lambda _: SimpleNamespace(warehouse_path=path))
    with pytest.raises(ValueError, match='warehouse_missing'): inspect(db_session)
    assert not path.exists()


def test_public_http_is_readonly_until_explicit_freeze_and_never_dispatches(prepared, db_session, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.scoring_facade import router
    from app.db.session import get_db
    from app.services import async_tasks
    monkeypatch.setattr(async_tasks, '_start_worker', lambda *args: pytest.fail('Plan must not dispatch a worker'))
    app = FastAPI(); app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: db_session
    url = '/api/v1/scoring/models/research-parent/production-admission'
    with TestClient(app) as client:
        read = client.get(url, params={'bundle_id': 'test-inputs'})
        assert read.status_code == 200, read.text
        assert not admission.PLAN_ROOT.exists()
        body = dict(bundle_id='test-inputs', expected_identity_sha256=read.json()['identity_sha256'])
        assert client.post(url+'/freeze', json={**body, 'force': True}).status_code == 422
        assert client.post(url+'/freeze', json={**body, 'expected_identity_sha256': '0'*64}).status_code == 409
        first = client.post(url+'/freeze', json=body)
        assert first.status_code == 200, first.text
        assert client.post(url+'/freeze', json=body).json()['frozen_plan'] == first.json()['frozen_plan']
        assert client.get(url, params={'bundle_id': '../escape'}).status_code == 422


def test_non_research_model_rejected_without_sql_mutation(db_session):
    from app.models.factor_model import FactorModelRun
    model = FactorModelRun(id='ordinary-model', status='validated')
    db_session.add(model); db_session.flush()
    with pytest.raises(ValueError, match='requires_research_parent'):
        admission._parent_context(db_session, model.id)
