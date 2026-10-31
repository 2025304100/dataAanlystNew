"""M07.3 public candidate ingestion and durable scope receipts on real DBs."""
from copy import deepcopy
from datetime import datetime,timezone,date
import json
import pytest
from app.services.governance_activity_sync import ActivitySyncService,activity_contract,install_activity_schema,capture_activity,RANK_METHOD
from app.services.governance_fact_contracts import canonical,WriterError,digest,numeric_spec
from app.services.governance_writer import FactWriter
from tests.test_governance_writer import system,candidate,annual_page,approval

pytestmark=pytest.mark.whitebox


def archive(product,rows,symbol='sh600001'):
    return canonical({'format':'activity-records-v1','product':product,'symbol':symbol,'rows':rows}).encode()


def setup(system,product='flow_history',profile=None):
    base,request,clock,_=system
    install_activity_schema(base.engine)
    contract=activity_contract(product,evidence=b'SYNTHETIC native field/unit and definition evidence',profile=profile)
    writer=FactWriter(base.engine,contracts=[contract],clock=clock)
    kwargs=dict(product=product,contract_version=contract.version,instrument_id=request['key']['instrument_id'],
        instrument_version=request['instrument_version'],calendar_version=request['calendar_version'],start='2026-06-01',
        end_exclusive='2026-06-03',source_uri='fixture://supplier/records',batch_id='A')
    return ActivitySyncService(writer),kwargs,clock


def flow(**kw):
    return {'日期':'2026-06-01','主力净流入-净额':'-10','超大单净流入-净额':'20',
        '大单净流入-净额':'-30','中单净流入-净额':'5','小单净流入-净额':'5'}|kw


def rank(**kw):return {'sc':'SH600001','rk':2,'observation_at':'2026-06-01T16:00:00+08:00'}|kw


def lhb(**kw):return {'代码':'600001','上榜日期':'2026-06-01','上榜原因':'A',
    '机构买入总额':'100','机构卖出总额':'40','机构买入净额':'60'}|kw


def seat_proof(identity):
    fact={'fact_id':'official-buy-1','seat_id':'seat-1','side':'buy','amount_cny':'100','reason_id':'A',
          'instrument_id':identity,'trade_date':'2026-06-01','institution':True}
    return {'source_uri':'https://www.sse.com.cn/synthetic-seat-proof','complete':True,'all_reasons':['A','B'],
            'facts':[fact,fact|{'reason_id':'B'},fact|{'fact_id':'official-sell-1','side':'sell','amount_cny':'40','reason_id':'B'}]}


def test_flow_identity_units_negative_values_and_restart_receipt(system):
    service,kw,_=setup(system)
    raw=archive(kw['product'],[flow()]);result=service.ingest(raw,**kw)
    fact=service.writer.fact(result['results'][0]['data_version'])
    assert fact['payload']['main']=='-10.00000000' and fact['payload']['super_large']=='20.00000000'
    assert fact['available_at'] is None and not result['coverage_complete']
    reloaded=ActivitySyncService(service.writer).receipt(result['receipt_id'])
    assert reloaded['capture_hash']==digest(raw) and reloaded['data_versions']==[fact['data_version']]
    again=service.ingest(raw,**(kw|{'batch_id':'B'}))
    assert again['receipt_id']==result['receipt_id'] and again['results']==result['results']


def test_main_only_profile_does_not_invent_other_buckets(system):
    service,kw,_=setup(system,profile='capital_flow/main')
    raw=archive(kw['product'],[{'日期':'2026-06-01','主力净流入-净额':'0'}])
    request=service.requests(raw,**kw)[0];result=service.writer.write(request,approval(service.writer,request))
    assert result['accepted']
    fact=service.writer.fact(result['data_version'])
    assert fact['payload']['main']=='0.00000000' and fact['payload']['large'] is None
    assert service.writer.current('capital_flow',result['logical_key'])['quality_status']=='partial'


@pytest.mark.parametrize('bad',['identity','float','wrong_symbol','unknown','date','duplicate'])
def test_bad_flow_capture_never_partially_writes(system,bad):
    service,kw,_=setup(system);rows=[flow()];symbol='sh600001'
    if bad=='identity':rows[0]['主力净流入-净额']='-9'
    elif bad=='float':rows[0]['主力净流入-净额']=1.1
    elif bad=='wrong_symbol':symbol='sh600002'
    elif bad=='unknown':rows[0]['main']='-10'
    elif bad=='date':rows[0]['日期']='2026-06-03'
    elif bad=='duplicate':rows.append(flow())
    with pytest.raises(ValueError):service.ingest(archive(kw['product'],rows,symbol),**kw)


def test_lhb_aggregate_without_seat_identity_stays_partial(system):
    service,kw,_=setup(system,'lhb_institution')
    request=service.requests(archive(kw['product'],[lhb()]),**kw)[0]
    result=service.writer.write(request)
    fact=service.writer.fact(result['data_version'])
    assert all(fact['payload'][n] is None for n in ('institution_buy','institution_sell','institution_net'))
    with pytest.raises(WriterError,match='QUALITY_BLOCKED'):service.writer.write(request,approval(service.writer,request))


def test_lhb_seat_union_deduplicates_same_fact_across_reasons(system):
    service,kw,_=setup(system,'lhb_institution')
    request=service.requests(archive(kw['product'],[lhb(seat_evidence=seat_proof(kw['instrument_id']))]),**kw)[0]
    result=service.writer.write(request,approval(service.writer,request))
    payload=service.writer.fact(result['data_version'])['payload']
    assert payload['institution_buy']=='100.00000000' and payload['institution_sell']=='40.00000000'
    assert payload['institution_net']=='60.00000000' and payload['dedup_rule_id']=='reason-seat-union-v1'


@pytest.mark.parametrize('bad',['conflict','authority','missing_reason','wrong_day','negative','ambiguous_seat','net'])
def test_invalid_lhb_dedup_proofs_fail_closed(system,bad):
    service,kw,_=setup(system,'lhb_institution');proof=seat_proof(kw['instrument_id']);row=lhb(seat_evidence=proof)
    if bad=='conflict':proof['facts'][1]['amount_cny']='101'
    elif bad=='authority':proof['source_uri']='https://example.com/seats'
    elif bad=='missing_reason':proof['all_reasons'].append('C')
    elif bad=='wrong_day':proof['facts'][0]['trade_date']='2026-06-02'
    elif bad=='negative':proof['facts'][0]['amount_cny']='-1'
    elif bad=='ambiguous_seat':proof['facts'][1]['fact_id']='another-buy-id'
    elif bad=='net':row['机构买入净额']='1000'
    with pytest.raises(ValueError):service.ingest(archive(kw['product'],[row]),**kw)


def test_rank_unknown_total_is_null_and_known_total_uses_frozen_formula(system):
    service,kw,_=setup(system,'hot_rank')
    raw=archive(kw['product'],[rank()]);result=service.ingest(raw,**kw)
    payload=service.writer.fact(result['results'][0]['data_version'])['payload']
    assert payload['rank']==2 and payload['total'] is None and payload['pct'] is None
    proof={'ranking_method':RANK_METHOD,'observation_at':rank()['observation_at'],'total':1000,'universe':'all_ranked_stocks'}
    raw=archive(kw['product'],[rank(total=1000,total_evidence=proof)])
    request=service.requests(raw,**kw)[0]
    result=service.writer.write(request)
    assert service.writer.fact(result['data_version'])['payload']['pct']=='99.9000000000'
    assert numeric_spec('pct','percentage_point')==(24,10)
    with pytest.raises(WriterError,match='NORMALIZED_UNIT_MISMATCH'):numeric_spec('pct','CNY')


@pytest.mark.parametrize('bad',[{'rk':0},{'rk':1.2},{'total':100},{'total':1},
    {'observation_at':'2026-06-02T19:00:00+08:00'},{'sc':'SH600002'},{'total_evidence':{}}])
def test_invalid_rank_or_unproven_population_is_rejected(system,bad):
    service,kw,_=setup(system,'hot_rank')
    with pytest.raises(ValueError):service.ingest(archive(kw['product'],[rank(**bad)]),**kw)


def test_rank_last_real_snapshot_never_uses_later_observation_or_unaccepted_stage(system):
    service,kw,clock=setup(system,'hot_rank')
    first=service.requests(archive(kw['product'],[rank()]),**kw)[0]
    service.writer.write(first)
    assert service.latest_rank(instrument_id=kw['instrument_id'],cutoff='2026-06-02T10:00:00Z')['status']=='missing'
    accepted=service.writer.write(first,approval(service.writer,first))
    clock.advance()
    later=service.requests(archive(kw['product'],[rank(rk=1,observation_at='2026-06-02T17:00:00+08:00')]),**kw)[0]
    new=service.writer.write(later,approval(service.writer,later))
    assert service.latest_rank(instrument_id=kw['instrument_id'],cutoff='2026-06-02T10:00:00Z')['data_version']==accepted['data_version']
    assert service.latest_rank(instrument_id=kw['instrument_id'],cutoff='2026-06-02T10:00:02Z')['data_version']==new['data_version']


def test_sparse_empty_receipt_requires_bound_response_and_never_sets_coverage(system):
    service,kw,_=setup(system,'lhb_institution');raw=archive(kw['product'],[])
    unknown=service.ingest(raw,**kw)
    assert unknown['outcome']=='empty_unverified' and not unknown['no_event_evidenced']
    scope={n:kw[n] for n in ('product','instrument_id','instrument_version','calendar_version','start','end_exclusive','source_uri')}
    proof=canonical({'scope':scope,'capture_hash':digest(raw),'response':{'success':True,'result':{'count':0,'data':[]}}}).encode()
    result=service.ingest(raw,**kw,empty_proof=proof)
    assert result['no_event_evidenced'] and not result['coverage_complete']
    assert service.receipt(result['receipt_id'])['outcome']=='empty_verified'
    wrong=json.loads(proof);wrong['scope']['start']='2026-05-31'
    with pytest.raises(WriterError,match='EMPTY_EVENT_EVIDENCE_INVALID'):service.ingest(raw,**kw,empty_proof=canonical(wrong).encode())


def test_batch_failure_rolls_back_facts_and_receipt(system,monkeypatch):
    service,kw,_=setup(system);raw=archive(kw['product'],[flow(),flow(日期='2026-06-02')])
    requests=service.requests(raw,**kw);original=service.writer.write;calls=[]
    def fail(*args,**kwargs):
        calls.append(True)
        if len(calls)==2:raise RuntimeError('injected second-row failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(service.writer,'write',fail)
    with pytest.raises(RuntimeError):service.ingest(raw,**kw)
    from sqlalchemy import select,func
    from app.db.governance_activity_schema import receipts
    from app.services.governance_fact_contracts import logical_identity
    assert service.writer.audit(logical_identity('capital_flow',requests[0]['key'])[0])['counts']['facts']==0
    with service.writer.engine.connect() as db:assert db.scalar(select(func.count()).select_from(receipts))==0


def test_activity_migration_is_additive_idempotent_and_retains_prior_fact(system):
    service,kw,_=setup(system)
    result=service.ingest(archive(kw['product'],[flow()]),**kw)
    assert install_activity_schema(service.writer.engine)['created_tables']==[]
    assert service.receipt(result['receipt_id'])['data_versions']==[result['results'][0]['data_version']]


@pytest.mark.parametrize('kind',['correction','withdrawal'])
def test_explicit_flow_revision_is_versioned_without_auto_replacing_accepted_result(system,kind):
    service,kw,clock=setup(system)
    original=service.requests(archive(kw['product'],[flow()]),**kw)[0]
    first=service.writer.write(original,approval(service.writer,original));old=service.writer.fact(first['data_version'])
    clock.advance()
    revision={'kind':kind,'token':None,'order':None,'order_evidence':None,'supersedes_version':first['data_version']}
    result=service.ingest(archive(kw['product'],[flow()]),**kw,revisions={'2026-06-01':revision})
    assert result['results'][0]['data_version']!=first['data_version']
    assert service.writer.fact(first['data_version'])==old
    assert service.writer.current('capital_flow',first['logical_key'])['data_version']==first['data_version']


def test_tampered_receipt_capture_is_detected(system):
    service,kw,_=setup(system)
    result=service.ingest(archive(kw['product'],[flow()]),**kw)
    from app.db.governance_activity_schema import receipts
    with service.writer.engine.begin() as db:
        db.execute(receipts.update().where(receipts.c.receipt_id==result['receipt_id']).values(capture_bytes=b'corrupted'))
    with pytest.raises(WriterError,match='RECEIPT_HASH_MISMATCH'):service.receipt(result['receipt_id'])


@pytest.mark.parametrize('failure',[ConnectionError('supplier unavailable'),InterruptedError('cancelled')])
def test_capture_failure_or_cancellation_is_propagated_without_fake_empty(monkeypatch,failure):
    def fail(**kwargs):raise failure
    monkeypatch.setattr('app.services.capital_flow_history_provider.fetch_capital_flow_history',fail)
    with pytest.raises(type(failure)):
        capture_activity(None,product='flow_history',symbol='sh600001',start='2026-06-01',end_exclusive='2026-06-03')


@pytest.mark.parametrize('product',['flow_history','lhb_institution','hot_rank'])
def test_existing_transport_capture_preserves_native_records_without_legacy_aggregation(monkeypatch,product):
    import pandas as pd
    now=datetime(2026,6,1,10,tzinfo=timezone.utc)
    if product=='flow_history':monkeypatch.setattr('app.services.capital_flow_history_provider.fetch_capital_flow_history',lambda **kw:pd.DataFrame([flow()]))
    elif product=='lhb_institution':monkeypatch.setattr('app.services.akshare_utils.call_akshare_with_retry',lambda *a,**kw:pd.DataFrame([lhb(),lhb(上榜原因='B')]))
    else:monkeypatch.setattr('app.services.hot_rank_data._fetch_hot_rank',lambda *a:(pd.DataFrame(),{'600001':json.dumps({'sc':'SH600001','rk':2})}))
    raw=capture_activity(None,product=product,symbol='sh600001',start='2026-06-01',end_exclusive='2026-06-03',clock=lambda:now)
    doc=json.loads(raw)
    assert doc['symbol']=='sh600001' and len(doc['rows'])==1
    if product=='lhb_institution':assert len(doc['rows'][0]['reason_rows'])==2 and '机构买入总额' not in doc['rows'][0]
    if product=='hot_rank':assert 'total' not in doc['rows'][0] and doc['rows'][0]['observation_at']==now.isoformat()
