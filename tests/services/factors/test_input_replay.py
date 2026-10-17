"""Actual temporary DuckDB ingestion/calculation/inference; no network or fitting."""
from pathlib import Path
import hashlib
import json

import pytest
from sqlalchemy import event
from tests.test_trusted_history_training_pipeline import _dataset
from app.services.factors import input_replay as replay
from app.services.factors.factor_engine import FrozenFactorSpec
from app.services.factors.scoring_bridge import evaluate_linear_factor_model

pytestmark=pytest.mark.whitebox

def _sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def test_real_linear_inference_has_direction_intercept_and_no_missing_imputation():
    row=lambda n:{'turnover_z20':{'normalized_value':n}}
    result=evaluate_linear_factor_model({'low':row(-1),'high':row(1),'missing':{},'null':row(None),'nan':row(float('nan'))},
        {'turnover_z20':-2},.5)
    assert result['alpha_raw']=={'low':2.5,'high':-1.5}
    assert result['alpha_scores']=={'high':0,'low':100}
    assert evaluate_linear_factor_model({'a':row(0),'b':row(0)},{'turnover_z20':1})['alpha_scores']=={'a':50,'b':50}

@pytest.fixture
def source_bundle(tmp_path,monkeypatch):
    ds_id,days,directory=_dataset(tmp_path,monkeypatch,day_count=28)
    monkeypatch.setattr(replay,'PROJECT',tmp_path)
    monkeypatch.setattr(replay,'BUNDLE_ROOT',tmp_path/'bundles')
    monkeypatch.setattr(replay,'REPORT_ROOT',tmp_path/'replays')
    source=tmp_path/'stage';source.mkdir()
    provider=tmp_path/'provider';provider.mkdir()
    rows=[dict(SECURITY_CODE='000001',SECUCODE='000001.SZ',CURRENCY='CNY',REPORT_DATE='2022-09-30',NOTICE_DATE='2022-11-01',ROEJQ=2),
        dict(SECURITY_CODE='000001',SECUCODE='000001.SZ',CURRENCY='CNY',REPORT_DATE='2023-09-30',NOTICE_DATE='2023-11-01',ROEJQ=5)]
    unused=dict(SECURITY_CODE='000001',SECUCODE='000001.SZ',CURRENCY=None,
        REPORT_DATE='1996-06-30',NOTICE_DATE=None,ROEJQ=None)
    (provider/'response.bin').write_text(json.dumps({'result':{'data':rows+[unused]}}),encoding='utf-8')
    meta=dict(symbol='000001',kind='financial',sha256=_sha(provider/'response.bin'))
    (provider/'receipt.json').write_text(json.dumps(meta),encoding='utf-8')
    evidence=dict(receipt_path=str(provider/'receipt.json'),receipt_sha256=_sha(provider/'receipt.json'),response_sha256=meta['sha256'])
    financial=[dict(symbol='000001',report_period=r['REPORT_DATE'],announcement_date=r['NOTICE_DATE'],report_type='financial_analysis',
        roe_ttm=r['ROEJQ'],net_profit=None,revenue=None,net_profit_yoy=None,revenue_yoy=None,
        financial_amount_unit='CNY',growth_unit='percent',evidence=dict(evidence))for r in rows]
    (source/'financial.json').write_text(json.dumps(financial),encoding='utf-8')
    (source/'flows.json').write_text('[]',encoding='utf-8')
    (source/'manifest.json').write_text(json.dumps(dict(files={k:_sha(source/k)for k in ('financial.json','flows.json')},
        financial_rows=2,financial_symbols=1,flow_rows=0,flow_symbols=0)),encoding='utf-8')
    m=replay.register_staged_bundle(source,bundle_id='test-inputs',dataset_id=ds_id,
        dataset_sha256=_sha(directory/'manifest.json'),label='Synthetic isolated input verification')
    return m,_sha(replay.BUNDLE_ROOT/'test-inputs/manifest.json'),days

def test_real_source_to_factor_to_score_report_is_sql_readonly(source_bundle,monkeypatch,db_session):
    manifest,sha,days=source_bundle
    specs=(FrozenFactorSpec('turnover_z20',1,1,'exclude',True,None,frozenset({'raw_daily_bars'})),)
    model=dict(id='saved-research-model',coefficients={'turnover_z20':-.25},feature_versions={'turnover_z20':1},intercept=.1)
    monkeypatch.setattr(replay,'load_model_contract',lambda db,model_id:(specs,model))
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement)
    event.listen(db_session.get_bind(),'before_cursor_execute',capture)
    try:
        result=replay.run_input_replay(db_session,bundle_id='test-inputs',manifest_sha256=sha,
            model_id='saved-research-model',replay_id='a'*32)
    finally:event.remove(db_session.get_bind(),'before_cursor_execute',capture)
    assert result['research_only'] and result['production_scores_published']==0
    assert result['scored_symbols']==24 and result['input_counts']['raw_financial_reports']==2
    assert next(r for r in result['coverage']if r['code']=='roe_yoy_growth')['eligible']==1
    assert next(r for r in result['coverage']if r['code']=='main_inflow_5d_ratio')['eligible']==0
    assert len(result['gaps']['no_flow_symbols'])==24
    assert all(abs(row['linear_signal']-(.1+row['features']['turnover_z20']['contribution']))<1e-10 for row in result['rows'])
    assert replay.read_input_replay_report('a'*32)==result
    assert not any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','ALTER'))for s in sql)
    # A second set of real factors with changed saved coefficients must change
    # the report's ordering, proving the displayed values are not canned/cache.
    model['coefficients']={'turnover_z20':.25}
    changed=replay.run_input_replay(db_session,bundle_id='test-inputs',manifest_sha256=sha,
        model_id='saved-research-model',replay_id='b'*32)
    assert result['rows'][0]['symbol']!=changed['rows'][0]['symbol']
    report_path=replay.REPORT_ROOT/('a'*32)/'report.json'
    report_path.write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='report_checksum'):replay.read_input_replay_report('a'*32)

def test_bundle_integrity_and_source_values_are_revalidated(source_bundle):
    _,sha,_=source_bundle
    root=replay.BUNDLE_ROOT/'test-inputs'
    manifest,records=replay._load_inputs('test-inputs',sha)
    rows=json.loads((root/'financial.json').read_text())
    rows[0]['roe_ttm']=999
    (root/'financial.json').write_text(json.dumps(rows),encoding='utf-8')
    with pytest.raises(ValueError,match='file_checksum'):replay._load_inputs('test-inputs',sha)
    # Updating a top-level digest cannot turn a changed normalized value into
    # provider evidence: the original response is parsed again independently.
    manifest['files']['financial.json']=_sha(root/'financial.json')
    (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='normalized_value_mismatch'):replay._load_inputs('test-inputs',_sha(root/'manifest.json'))

@pytest.mark.parametrize('value',['../escape','C:/other','bad/name','A'*81])
def test_bundle_paths_are_not_user_supplied_paths(value):
    with pytest.raises(ValueError):replay._bundle_dir(value)

def test_public_request_is_idempotent_and_get_never_runs_patrol(source_bundle,monkeypatch,db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from app.api.routes.scoring_facade import router
    from app.db.session import get_db
    from app.services import async_tasks
    from app.services.factors import input_replay_task as jobs
    from app.models.async_task import AsyncTaskRecord
    factory=sessionmaker(bind=db_session.get_bind(),expire_on_commit=False)
    monkeypatch.setattr(jobs,'get_session_local',lambda:factory)
    monkeypatch.setattr(async_tasks,'_pick_session_factory',lambda _:factory)
    monkeypatch.setattr(jobs,'load_model_contract',lambda *_:None)
    starts=[]
    monkeypatch.setattr(async_tasks,'_start_worker',lambda task_id,runner:starts.append(task_id))
    monkeypatch.setattr(async_tasks,'_run_patrol',lambda _:(_ for _ in ()).throw(AssertionError('patrol must not run')))
    app=FastAPI();app.include_router(router,prefix='/api/v1')
    app.dependency_overrides[get_db]=lambda:db_session
    _,sha,_=source_bundle
    payload=dict(bundle_id='test-inputs',manifest_sha256=sha,model_id='research',request_id='c'*32)
    with TestClient(app)as client:
        first=client.post('/api/v1/scoring/input-replays',json=payload)
        assert first.status_code==202,first.text
        assert client.post('/api/v1/scoring/input-replays',json=payload).json()['id']=='c'*32
        assert starts==['c'*32]
        assert client.post('/api/v1/scoring/input-replays',json={**payload,'model_id':'different'}).status_code==409
        assert client.get('/api/v1/scoring/input-replays/'+'c'*32).json()['status']=='queued'
        assert client.get('/api/v1/scoring/input-replays/'+'d'*32).status_code==404
        assert client.get('/api/v1/scoring/input-replays/'+'c'*32+'/report').status_code==404
        assert client.post('/api/v1/scoring/input-replays',json={**payload,'bundle_id':'../escape'}).status_code==422

def test_copied_receipt_cannot_be_rebound_to_another_stock(source_bundle):
    _,sha,_=source_bundle
    root=replay.BUNDLE_ROOT/'test-inputs'
    manifest,rows=replay._load_inputs('test-inputs',sha)
    records=json.loads((root/'financial.json').read_text())
    records[1]['symbol']='000002'
    (root/'financial.json').write_text(json.dumps(records),encoding='utf-8')
    manifest['files']['financial.json']=_sha(root/'financial.json')
    (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='binding_mismatch'):
        replay._load_inputs('test-inputs',_sha(root/'manifest.json'))

def test_unknown_currency_in_a_consumed_observation_still_fails(source_bundle):
    _,_,_=source_bundle
    root=replay.BUNDLE_ROOT/'test-inputs'
    response=root/'receipts/000001/financial/response.bin'
    value=json.loads(response.read_text());value['result']['data'][0]['CURRENCY']=None
    response.write_text(json.dumps(value),encoding='utf-8')
    receipt=response.parent/'receipt.json'
    meta=json.loads(receipt.read_text());meta['sha256']=_sha(response)
    receipt.write_text(json.dumps(meta),encoding='utf-8')
    financial=root/'financial.json';rows=json.loads(financial.read_text())
    for row in rows:row['evidence'].update(receipt_sha256=_sha(receipt),response_sha256=_sha(response))
    financial.write_text(json.dumps(rows),encoding='utf-8')
    manifest=json.loads((root/'manifest.json').read_text());manifest['files']['financial.json']=_sha(financial)
    (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='instrument_or_currency'):
        replay._load_inputs('test-inputs',_sha(root/'manifest.json'))
