"""Explicit release confirmation through HTTP, using disposable evidence only."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.test_turnover_v3_public_warehouse_lifecycle import turnover_client

pytestmark = pytest.mark.whitebox


@pytest.fixture
def release_candidate(turnover_client, db_session, tmp_path, monkeypatch):
    """Real receipt parsing, frozen v3, research fit, candidate fit and journals.

    Only the provider response and wall clock are synthetic. Every domain gate,
    replay, coefficient, target and cash calculation uses production code.
    """
    from datetime import datetime, timezone
    import pandas as pd
    from app.services import market_history_evidence as history
    from app.services.factors import input_replay, production_admission as admission
    from app.services.factors import production_execution as execution
    from app.services.factors import production_prediction_journal as journal
    from app.services.factors import production_cost_evaluation as cost
    from app.services.factors import production_independent_evidence as evidence
    from app.services.factors.production_candidate_task import run_candidate
    from app.services.factors.definitions import seed_factor_definitions
    from tests.test_trusted_history_training_pipeline import _frozen_set
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    from tests.test_turnover_v3_public_warehouse_lifecycle import FORMULA
    root = tmp_path / 'tmp'; root.mkdir()
    monkeypatch.setattr(history, 'TRUSTED_HISTORY_DATASET_ROOT', root / 'datasets')
    monkeypatch.setattr(input_replay, 'PROJECT', tmp_path)
    monkeypatch.setattr(input_replay, 'BUNDLE_ROOT', root / 'bundles')
    monkeypatch.setattr(admission, 'PROJECT', tmp_path)
    monkeypatch.setattr(admission, 'PLAN_ROOT', root / 'plans')
    monkeypatch.setattr(execution, 'EXECUTION_ROOT', root / 'executions')
    monkeypatch.setattr(journal, 'JOURNAL_ROOT', root / 'journals')
    monkeypatch.setattr(evidence, 'REPORT_ROOT', root / 'reports')
    boundary = datetime.now(timezone.utc).astimezone(journal.EXCHANGE_ZONE).date()
    # A weekday boundary with 310 preceding sessions and 55 following sessions.
    days = tuple(pd.bdate_range(end=boundary, periods=310).date)
    assert days[-1] == boundary
    days += tuple(pd.bdate_range(start=pd.Timestamp(boundary) + pd.Timedelta(days=1), periods=55).date)
    clock = [datetime.combine(boundary, datetime.min.time(), tzinfo=timezone.utc).replace(hour=8)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(history, 'datetime', Clock)
    monkeypatch.setattr(admission, 'datetime', Clock)
    monkeypatch.setattr(cost, 'datetime', Clock)
    monkeypatch.setattr(journal, '_now', lambda: clock[0])
    from app.services.security_status import local_import
    from app.api.routes import security_status
    monkeypatch.setattr(local_import, 'datetime', Clock)
    turnover_client.app.include_router(security_status.router, prefix='/api/v1')
    symbols = tuple(f'{n:06d}' for n in range(1, 25))
    frames = {}
    for index, symbol in enumerate(symbols):
        slope = index % 3 - 1; previous = 10.; rows = []
        for n, day in enumerate(days):
            close = previous * (1 + .001 * slope)
            rows.append({'date': str(day), 'code': 'sz.' + symbol, 'open': str(previous),
                'high': str(max(previous, close) * 1.01), 'low': str(min(previous, close) * .99),
                'close': str(close), 'volume': '1000000', 'amount': str(close * 1000000),
                'turn': str(10 + slope * .02 * (n + 1)), 'adjustflag': '2'})
            previous = close
        frames[symbol] = pd.DataFrame(rows)
    stage = root / 'stage'; stage.mkdir()
    for filename in ('financial.json', 'flows.json'): (stage / filename).write_text('[]')
    (stage / 'manifest.json').write_text(admission.canonical({'files':
        {name: input_replay._sha(stage / name) for name in ('financial.json', 'flows.json')},
        'financial_rows': 0, 'financial_symbols': 0, 'flow_rows': 0, 'flow_symbols': 0}))
    def dataset(name, selected, *, raw=False):
        directory = history.TRUSTED_HISTORY_DATASET_ROOT / name
        refs = []
        for symbol in symbols:
            frame = frames[symbol][frames[symbol].date.isin([str(d) for d in selected])]
            for adjust in (('qfq', '') if raw else ('qfq',)):
                frame = frame.copy(); frame['adjustflag'] = '2' if adjust else '3'
                refs.append(history.capture_baostock_history(artifact_root=directory / 'receipts',
                    capture_id=symbol + ('-qfq' if adjust else '-raw'),
                    request=history.HistoryRequest('sz' + symbol, selected[0], selected[-1], adjust),
                    sdk_version='isolated-synthetic-provider', raw_frame=frame))
        history.write_trusted_history_dataset(dataset_id=name, receipts=refs,
            cohort_symbols=symbols, calendar=selected, plan_sha256='a' * 64)
        input_replay.register_staged_bundle(stage, bundle_id=name, dataset_id=name,
            dataset_sha256=input_replay._sha(directory / 'manifest.json'), label='隔离测试资料')
        return name
    default_path, _, _, _ = _seed_local_market(db_session, tmp_path, day_count=2)
    from app.services.factors.store import FactorWarehouse
    FactorWarehouse(default_path).initialize()
    seed_factor_definitions(db_session); db_session.commit()
    first = turnover_client.post('/api/v1/factors/turnover_z20/versions', json={
        'formula_expr': '(' + FORMULA + ') + 0', 'params': {}, 'direction': 'nonlinear',
        'source_mapping': {'table': 'raw_daily_bars', 'field': 'turnover_rate'},
        'postprocess': {'winsorize': {'method': 'mad', 'mad_multiplier': 3.},
                        'zscore': {'ddof': 0}, 'missing_policy': 'exclude'}})
    assert first.status_code == 201, first.text
    set_id = _frozen_set(turnover_client, db_session)
    from app.models.factor_evaluation import FactorSetMember
    from sqlalchemy import select
    actual = db_session.scalar(select(FactorSetMember.factor_version).where(FactorSetMember.factor_set_id == set_id))
    assert actual == 3, (first.json(), actual)
    training = dataset('training-input', days[:310])
    response = turnover_client.post('/api/v1/scoring/tasks', json={
        'trusted_history_dataset_id': training, 'train_model': True, 'materialize_scores': False,
        'factor_set_id': set_id, 'window_days': 250, 'validation_days': 50})
    assert response.status_code == 200, response.text
    task = turnover_client.get('/api/v1/scoring/tasks/' + response.json()['id']).json()
    assert task['status'] == 'done', task
    parent_id = task['result']['model']['model_run_id']
    review = admission.inspect_production_admission(db_session, parent_id, bundle_id=training)
    assert review['can_freeze'], review
    frozen = admission.freeze_production_admission(db_session, parent_id, bundle_id=training,
        expected_identity_sha256=review['identity_sha256'])
    plan_id = frozen['frozen_plan']['plan_id']
    # This router fixture does not expose portfolio CRUD; create the input boundary record.
    from app.models.portfolio import Portfolio
    p = Portfolio(name='隔离验证组合', account_type='simulated', total_capital=500000., investable_ratio=.9,
                  cash_reserve_ratio=.1, currency='CNY', buy_fee_pct=0., sell_fee_pct=0.)
    db_session.add(p); db_session.commit()
    execution_review = execution.review_execution(db_session, parent_id, bundle_id=training, portfolio_id=p.id)
    locked = execution.freeze_execution(db_session, parent_id, bundle_id=training,
        portfolio_id=p.id, expected_sha256=execution_review['sha256'])
    candidate = run_candidate(db_session, {'plan_id': plan_id, 'execution_sha256': locked['sha256']})
    assert candidate['candidate']['status'] == 'pending_validation', candidate
    cost.freeze_cost_policy(db_session, plan_id)
    for index in range(310, 360):
        clock[0] = datetime.combine(days[index], datetime.min.time(), tzinfo=timezone.utc).replace(hour=8)
        bundle_id = dataset('daily-' + str(index), days[index-19:index+1])
        clock[0] = clock[0].replace(hour=9)
        journal.write_daily_prediction(db_session, plan_id, bundle_id=bundle_id)
    clock[0] = datetime.combine(days[-1], datetime.min.time(), tzinfo=timezone.utc).replace(hour=8)
    mature = dataset('mature-input', days, raw=True)
    clock[0] = clock[0].replace(hour=9)
    without_status = evidence.review_independent_evidence(db_session, plan_id, bundle_id=mature,
        require_execution_constraints=True)
    assert not without_status['ready']
    assert any('历史状态' in item or '股票' in item for item in without_status['blockers'])
    import csv, io
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(['symbol','trade_date','is_st','is_suspended','is_delisting_period','is_listed',
        'listing_date','delisting_date','price_limit_mode','upper_limit_price','lower_limit_price','tick_size'])
    for symbol in symbols:
        for index in range(310,365):
            previous_close = float(frames[symbol].iloc[index-1]['close'])
            writer.writerow([symbol,str(days[index]),0,0,0,1,'2000-01-01','',
                'LIMITED',f'{previous_close*1.1:.2f}',f'{previous_close*.9:.2f}','0.01'])
    body = {'csv_text':output.getvalue(),'source_name':'provider_archive',
        'source_reference':'isolated original dated records','source_version':'archive-v1',
        'source_updated_at':clock[0].isoformat()}
    preview = turnover_client.post('/api/v1/security-status/import/preview',json=body)
    assert preview.status_code == 200 and preview.json()['can_import'], preview.text
    imported = turnover_client.post('/api/v1/security-status/import',json={**body,
        'preview_sha256':preview.json()['content_sha256'],'confirmed':True})
    assert imported.status_code == 200, imported.text
    check = evidence.review_independent_evidence(db_session, plan_id, bundle_id=mature,
        require_execution_constraints=True)
    assert check['ready'], check
    # Optional review artifact: an actual temporary DB checkpoint before any
    # release, so a failed long run can replay the same immutable evidence.
    import os, sqlite3
    from pathlib import Path
    checkpoint = os.environ.get('PRODUCTION_RELEASE_SNAPSHOT_PATH')
    if checkpoint:
        target = Path(checkpoint).resolve()
        assert target.is_relative_to((Path.cwd() / 'tmp').resolve())
        target.parent.mkdir(parents=True,exist_ok=True)
        source = sqlite3.connect(str(db_session.get_bind().url.database))
        try:
            with sqlite3.connect(target.with_suffix('.sqlite')) as destination:
                source.backup(destination)
        finally:
            source.close()
        import json
        target.write_text(json.dumps({'source_project':str(tmp_path),'plan_id':plan_id,
            'bundle_id':mature,'candidate_model_id':candidate['candidate']['model_run_id'],
            'clock':clock[0].isoformat(),'database':str(target.with_suffix('.sqlite'))},indent=2),encoding='utf-8')
    report = evidence.run_independent_evaluation(db_session, plan_id,
        bundle_id=mature, expected_sha256=check['sha256'],require_execution_constraints=True)
    assert report['evaluation']['evidence_pass'], report
    return turnover_client, plan_id, mature, candidate['candidate']['model_run_id'], report


def test_confirmation_cannot_be_omitted_or_replaced_with_client_evidence(db_session):
    from app.api.routes.scoring_facade import router
    from app.db.session import get_db
    app = FastAPI(); app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    url = '/api/v1/scoring/production-candidates/' + 'a' * 64 + '/confirm-release'
    body = {'bundle_id': 'verified-input', 'expected_sha256': 'b' * 64, 'confirmed': True}
    for invalid in ({k:v for k,v in body.items() if k != 'confirmed'},
                    {**body, 'confirmed': False}, {**body, 'confirmed': 1}, {**body, 'force': True},
                    {**body, 'evidence_pass': True}, {**body, 'model_id': 'another'}):
        response = client.post(url, json=invalid)
        assert response.status_code == 422, response.text


def test_real_fifty_date_evidence_requires_explicit_confirmation_before_activation(release_candidate, db_session):
    client, plan_id, bundle, model_id, _ = release_candidate
    url = '/api/v1/scoring/production-candidates/' + plan_id
    before = client.get('/api/v1/factor-models/runtime').json()
    review = client.get(url + '/release-review', params={'bundle_id': bundle})
    assert review.status_code == 200, review.text
    assert review.json()['ready'] and not review.json()['model_activated']
    assert client.get('/api/v1/factor-models/runtime').json() == before
    body = {'bundle_id': bundle, 'expected_sha256': review.json()['sha256'], 'confirmed': True}
    from app.models.factor_model import FactorModelRun
    from app.models.portfolio import Portfolio
    from app.services.factors import production_execution as execution
    from app.services.factors import production_admission as admission
    from app.services.factors import input_replay
    import json
    # Each stale review must fail before a release write or expensive replay.
    model = db_session.get(FactorModelRun, model_id)
    original_hp = model.hyperparameters_json
    hp = json.loads(original_hp); hp['final_intercept'] += .01
    model.hyperparameters_json = json.dumps(hp); db_session.commit()
    assert client.post(url + '/confirm-release', json=body).status_code == 409
    model.hyperparameters_json = original_hp; db_session.commit()
    p = db_session.get(Portfolio, execution.load_execution(plan_id)[0]['cost']['portfolio_id'])
    p.buy_fee_pct = .01; db_session.commit()
    assert client.post(url + '/confirm-release', json=body).status_code == 409
    p.buy_fee_pct = 0.; db_session.commit()
    from app.models.security_status import SecurityStatusDaily
    from sqlalchemy import select, event
    status = db_session.scalar(select(SecurityStatusDaily).order_by(SecurityStatusDaily.trade_date).limit(1))
    status.is_suspended = True; db_session.commit()
    assert client.post(url + '/confirm-release', json=body).status_code == 409
    status.is_suspended = False; db_session.commit()
    for path in (admission.PLAN_ROOT / (plan_id+'.json'), input_replay.BUNDLE_ROOT / bundle / 'financial.json'):
        original = path.read_bytes(); path.write_bytes(b'{}')
        assert client.post(url + '/confirm-release', json=body).status_code in (404,409)
        path.write_bytes(original)
    assert client.get('/api/v1/factor-models/runtime').json() == before
    def fail_final_commit(_session):
        raise RuntimeError('injected final release commit failure')
    event.listen(db_session,'before_commit',fail_final_commit)
    try:
        with pytest.raises(RuntimeError,match='injected final release commit failure'):
            client.post(url + '/confirm-release',json=body)
    finally:
        event.remove(db_session,'before_commit',fail_final_commit)
    assert client.get('/api/v1/factor-models/runtime').json() == before
    rolled_back = client.get('/api/v1/factor-models/' + model_id).json()
    assert rolled_back['status'] == 'pending_validation' and not rolled_back['production_eligible']
    assert not any(row['action'] in ('activate','production_confirm') for row in rolled_back['audit'])
    response = client.post(url + '/confirm-release', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['model_activated'] and response.json()['qualification_issued']
    runtime = client.get('/api/v1/factor-models/runtime').json()
    assert runtime['active_model_run_id'] == model_id and runtime['weight_mode'] == 'ridge'
    from time import perf_counter
    started = perf_counter()
    assert client.get('/api/v1/factor-models/' + model_id).json()['production_eligible']
    readiness_seconds = perf_counter() - started
    from pathlib import Path
    timing = Path('tmp/business-closure-followup/production-release-readiness-timing.json')
    timing.parent.mkdir(parents=True,exist_ok=True)
    timing.write_text(json.dumps({'readiness_seconds':readiness_seconds,'signal_dates':50,
        'symbols':24,'reused_verified_test_evidence':False}),encoding='utf-8')
    assert client.post(url + '/confirm-release', json=body).status_code == 200
    assert client.get('/api/v1/factor-models/runtime').json() == runtime
    # A changed journal is not blessed by the persisted validated status.
    from app.services.factors import production_prediction_journal as journal
    path = next((journal.JOURNAL_ROOT / plan_id).glob('*.json'))
    original = path.read_bytes(); path.write_bytes(b'{}')
    detail = client.get('/api/v1/factor-models/' + model_id).json()
    assert not detail['production_eligible'] and detail['readiness_code'] == 'MODEL_RELEASE_EVIDENCE_CHANGED'
    path.write_bytes(original)
    assert client.get('/api/v1/factor-models/' + model_id).json()['production_eligible']
