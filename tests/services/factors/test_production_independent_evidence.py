"""Receipt/replay proofs plus isolated synthetic target/cash orchestration."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.factors import production_independent_evidence as evidence
from app.services.factors import production_prediction_journal as journal
from app.services.factors import production_execution as execution
from app.services.factors import production_admission as admission
from app.services.factors import production_cost_evaluation as cost
from tests.services.factors.test_production_prediction_journal import daily


def test_real_journal_replay_rejects_rehashed_forged_predictions(daily,db_session):
    contract,candidate,_,_ = daily
    envelope = journal.write_daily_prediction(db_session,contract['plan_id'],bundle_id='daily-input')
    signal = evidence.replay_journal(db_session,contract,candidate,envelope)
    assert len(signal['predictions']) == 24
    forged = deepcopy(envelope)
    forged['prediction']['predictions']['000001']['linear_signal'] = 99.
    forged['sha256'] = admission.fingerprint(forged['prediction'])
    # A matching JSON checksum is not enough: the actual factor must replay.
    with pytest.raises(ValueError,match='does_not_replay'):
        evidence.replay_journal(db_session,contract,candidate,forged)


@pytest.fixture
def prospective(tmp_path,monkeypatch):
    days = tuple(pd.bdate_range('2024-01-02',periods=56).date)
    symbols = [f'{n:06d}' for n in range(1,25)]
    contract = {'plan_id':'a'*64,'protocol':{'observed_through':str(days[0]),'symbols':symbols,
        'factor_set_id':'frozen','feature_versions':{'turnover_z20':1}},
        'cost':execution.normalize_portfolio_cost(SimpleNamespace(id=1,currency='CNY',total_capital=500000.,
            investable_ratio=.9,buy_fee_pct=0.,sell_fee_pct=0.))}
    candidate = {'model_id':'pending','coefficients':{'turnover_z20':-.25},'intercept':.1,
                 'feature_versions':{'turnover_z20':1}}
    source = {'bundle_id':'mature-input','dataset_id':'synthetic','dataset_manifest_sha256':'b'*64,'symbols':symbols}
    monkeypatch.setattr(journal,'_candidate',lambda *args:(deepcopy(contract),deepcopy(candidate)))
    monkeypatch.setattr(admission,'_bundle_context',lambda *args:(deepcopy(source),None,days))
    monkeypatch.setattr(cost,'load_cost_policy',lambda *args:{'sha256':'c'*64,'record':{'candidate':candidate}})
    monkeypatch.setattr(journal,'JOURNAL_ROOT',tmp_path/'journals')
    monkeypatch.setattr(evidence,'REPORT_ROOT',tmp_path/'reports')
    monkeypatch.setattr(journal,'_now',lambda:datetime(2030,1,1,9,tzinfo=timezone.utc))
    for day in days[1:52]:
        report = {'plan_id':contract['plan_id'],'signal_date':str(day),'candidate':candidate,
            'issued_at':datetime.combine(day,datetime.min.time(),tzinfo=journal.EXCHANGE_ZONE).replace(hour=17).isoformat(),
            'qualification_certificate':False}
        execution._publish(journal.JOURNAL_ROOT/contract['plan_id']/(str(day)+'.json'),
            {'prediction':report,'sha256':admission.fingerprint(report)})
    return contract,candidate,source,days


def test_missing_first_day_cannot_be_replaced_with_the_51st(prospective):
    contract,_,_,days = prospective
    path = journal.JOURNAL_ROOT/contract['plan_id']/(str(days[1])+'.json')
    # Remove a disposable fixture file, not any business artifact.
    path.unlink()
    review = evidence.review_independent_evidence(None,contract['plan_id'],bundle_id='mature-input')
    assert not review['ready'] and review['missing_journal_dates'] == [str(days[1])]
    assert review['identity']['signal_dates'] == [str(d) for d in days[1:51]]
    assert str(days[51]) not in review['identity']['signal_dates']
    with pytest.raises(ValueError,match='not_ready'):
        evidence.run_independent_evaluation(None,contract['plan_id'],bundle_id='mature-input',expected_sha256=review['sha256'])
    assert not evidence.REPORT_ROOT.exists()


def test_immature_t_plus_5_does_not_become_a_zero_return(prospective,monkeypatch):
    contract,_,_,days = prospective
    monkeypatch.setattr(journal,'_now',lambda:datetime.combine(days[50],datetime.min.time(),tzinfo=timezone.utc).replace(hour=9))
    review = evidence.review_independent_evidence(None,contract['plan_id'],bundle_id='mature-input')
    assert not review['ready'] and review['mature_signal_count'] == 45
    assert 'FIRST_50_T_PLUS_5_OUTCOMES_NOT_MATURE' in review['blockers']


def test_public_review_and_not_ready_post_never_create_tasks(prospective,monkeypatch,db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.scoring_facade import router
    from app.db.session import get_db
    from app.services import async_tasks
    contract,_,_,days = prospective
    (journal.JOURNAL_ROOT/contract['plan_id']/(str(days[1])+'.json')).unlink()
    monkeypatch.setattr(async_tasks,'create_async_task_once',lambda *a,**k:pytest.fail('incomplete evidence must not dispatch'))
    app = FastAPI();app.include_router(router,prefix='/api/v1');app.dependency_overrides[get_db] = lambda:db_session
    client = TestClient(app);url = '/api/v1/scoring/production-candidates/'+contract['plan_id']
    review = client.get(url+'/independent-review?bundle_id=mature-input').json()
    assert review['read_only'] and not review['ready']
    body = {'bundle_id':'mature-input','expected_sha256':review['sha256']}
    assert client.post(url+'/independent-evaluation',json=body).status_code == 409
    for extra in ({'signal_dates':[]},{'commission':0.},{'force':True},{'activate':True}):
        assert client.post(url+'/independent-evaluation',json={**body,**extra}).status_code == 422


def test_duplicate_dispatch_has_one_deterministic_task_identity(prospective,monkeypatch):
    from app.services import async_tasks
    from app.services.factors.production_independent_task import submit_independent_evaluation
    contract,_,_,_ = prospective;seen=[];started=[]
    def create_once(kind,payload,**kwargs):
        seen.append((kind,payload,kwargs));return SimpleNamespace(id=kwargs['task_id']),len(seen)==1
    monkeypatch.setattr(async_tasks,'create_async_task_once',create_once)
    monkeypatch.setattr(async_tasks,'_start_worker',lambda *args:started.append(args))
    review = evidence.review_independent_evidence(None,contract['plan_id'],bundle_id='mature-input')
    assert review['ready']
    for _ in range(2):
        submit_independent_evaluation(None,contract['plan_id'],bundle_id='mature-input',expected_sha256=review['sha256'])
    assert seen[0] == seen[1] and len(started) == 1
    with pytest.raises(ValueError,match='review_changed'):
        submit_independent_evaluation(None,contract['plan_id'],bundle_id='mature-input',expected_sha256='f'*64)


def test_raw_receipt_units_adjustment_time_and_duplicate_conflicts(tmp_path):
    from app.services.market_history_evidence import capture_baostock_history,HistoryRequest
    days = tuple(pd.bdate_range('2024-01-02',periods=6).date)
    root = tmp_path/'source';root.mkdir()
    def capture(name,adjust,close):
        raw = pd.DataFrame([{'date':str(d),'code':'sz.000001','open':'10','high':'11','low':'9',
            'close':str(close),'volume':'1000000','amount':'10000000','turn':'1',
            'adjustflag':'3' if adjust=='' else '2'} for d in days])
        ref = capture_baostock_history(artifact_root=root/'receipts',capture_id=name,
            request=HistoryRequest('sz000001',days[0],days[-1],adjust),sdk_version='synthetic-test',raw_frame=raw)
        ref['path'] = str(__import__('pathlib').Path(ref['path']).relative_to(root))
        return ref
    raw = capture('raw','',10);qfq = capture('qfq','qfq',10)
    def dataset(refs):
        (root/'manifest.json').write_text(admission.canonical({'schema_version':1,'evidence':{'receipts':refs}}))
        return SimpleNamespace(directory=root,manifest_sha256=evidence._sha(root/'manifest.json'),symbols=('000001',))
    ds = dataset([raw,qfq])
    prices,refs = evidence.verified_raw_prices(ds,set(days),as_of=datetime.now(timezone.utc)+timedelta(seconds=1))
    assert len(prices) == 6 and len(refs) == 1
    assert prices[('000001',days[0])]['volume'] == 1000000
    with pytest.raises(ValueError,match='time_or_unit'):
        evidence.verified_raw_prices(ds,set(days),as_of=datetime(2020,1,1,tzinfo=timezone.utc))
    with pytest.raises(ValueError,match='unadjusted_prices_missing'):
        evidence.verified_raw_prices(dataset([qfq]),set(days),as_of=datetime.now(timezone.utc)+timedelta(seconds=1))
    conflict = capture('raw-conflict','',10.01)
    with pytest.raises(ValueError,match='conflicting_raw'):
        evidence.verified_raw_prices(dataset([raw,conflict]),set(days),as_of=datetime.now(timezone.utc)+timedelta(seconds=1))


@pytest.mark.parametrize('direction',[1,-1])
def test_synthetic_orchestration_preserves_pass_and_failure_without_qualification(
    prospective,monkeypatch,tmp_path,db_session,direction,
):
    # Real verified qfq dataset, target engine and matcher. The 50 original
    # journal-replay boundary and raw receipt proof are tested separately above;
    # mocked returns here isolate target/IC/cash/report orchestration only.
    from tests.test_trusted_history_training_pipeline import _dataset
    from app.services import market_history_evidence as history
    ds_id,days,_ = _dataset(tmp_path,monkeypatch,day_count=56)
    dataset = history.load_trusted_history_dataset(ds_id)
    contract,candidate,source,calendar = prospective
    source.update(dataset_id=ds_id,dataset_manifest_sha256=dataset.manifest_sha256)
    raw = {(r.symbol,r.trade_date.date()):{k:float(getattr(r,k)) for k in ('open','high','low','close','volume','amount')}
        for r in dataset.bars.itertuples()}
    calls=[]
    def replay(db,c,model,envelope):
        day = __import__('datetime').date.fromisoformat(envelope['prediction']['signal_date']);calls.append(day)
        return {'date':day,'predictions':{s:direction*((int(s)-1)%3-1) for s in dataset.symbols}}
    monkeypatch.setattr(evidence,'replay_journal',replay)
    monkeypatch.setattr(evidence,'verified_raw_prices',lambda *a,**k:(raw,[{'receipt_sha256':'d'*64}]))
    review = evidence.review_independent_evidence(db_session,contract['plan_id'],bundle_id='mature-input')
    report = evidence.run_independent_evaluation(db_session,contract['plan_id'],bundle_id='mature-input',expected_sha256=review['sha256'])
    result = report['evaluation']
    assert len(calls)==50 and result['evidence_pass'] == (direction==1)
    assert result['statistical_pass'] == (direction==1) and result['net_cost_pass'] == (direction==1)
    assert not result['qualification_certificate'] and not result['model_activated']
    assert result['production_scores_published']==0
    again = evidence.run_independent_evaluation(db_session,contract['plan_id'],bundle_id='mature-input',expected_sha256=review['sha256'])
    assert again == report and len(calls)==100  # Cached reports still replay all proofs.
    summaries=evidence.saved_report_summaries(contract,candidate,'c'*64)
    assert len(summaries)==1 and summaries[0]['evidence_pass']==(direction==1)
    assert summaries[0]['release_reverification_required']
    from app.services.factors import production_independent_task as task
    payload={'plan_id':contract['plan_id'],'expected_sha256':review['sha256'],'bundle_id':'mature-input'}
    compact=task.compact_result(payload,report)
    assert len(json.dumps(compact))<2048 and 'traces' not in json.dumps(compact)
    monkeypatch.setattr(task,'get_independent_task',lambda *args:SimpleNamespace(status='done',payload_json=payload,result=compact))
    read=task.get_independent_report(db_session,'a'*32)
    assert read['report']==report and read['release_reverification_required']
    compact['report_sha256']='0'*64
    with pytest.raises(ValueError,match='task_binding_changed'):
        task.get_independent_report(db_session,'a'*32)
