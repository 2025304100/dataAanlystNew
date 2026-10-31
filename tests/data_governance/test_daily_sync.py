"""M07.1 candidate ingress, real SQLite/MySQL; captures are synthetic SDK records."""
from copy import deepcopy
import pytest
from app.services.governance_daily_sync import DailyBarSyncService
from app.services.governance_fact_contracts import WriterError, WriterContract, canonical
from app.services.governance_writer import FactWriter
from tests.test_governance_writer import system, candidate, annual_page, approval

pytestmark = pytest.mark.whitebox


def capture(contract, rows, symbol='sh600001', **overrides):
    adapter=contract.document['adapter']
    return canonical(dict(format='sdk-wire-records-v1',provider=adapter['provider'],endpoint=adapter['endpoint'],
        symbol=symbol,request_basis='raw',response_basis='raw',rows=rows,**overrides)).encode()


def args(request):
    return {k:request[k] for k in ('contract_version','instrument_version','calendar_version','source_uri','batch_id')} | {
        'instrument_id':request['key']['instrument_id'],'start':'2026-06-01','end_exclusive':'2026-06-03'}


def row(request,on='2026-06-01'):
    return {'trade_date':on,'wire_record':deepcopy(request['wire_record'])}


@pytest.mark.parametrize('source', ['eastmoney','sina','tencent','eastmoney_etf'])
def test_units_source_and_raw_capture_are_preserved_without_auto_approval(system,source):
    writer,request,clock,contract=system
    symbol='sh600001'
    if source=='eastmoney_etf':
        from tests.test_governance_master_data import publish,member,code
        from app.services.governance_master_data import MasterDataService
        master=MasterDataService(writer.engine)
        identity=master.register_identity('authority:fund-A',asset_type='etf',exchange='SSE')
        request['key']['instrument_id']=identity
        request['instrument_version']=publish(master,[member(identity)],[code(identity,'510300')],[],expected_head=request['instrument_version'])
        symbol='sh510300'
    if source!='eastmoney':
        provider='eastmoney' if source=='eastmoney_etf' else source
        endpoint={'sina':'stock_zh_a_daily','tencent':'stock_zh_a_hist_tx','eastmoney_etf':'fund_etf_hist_em'}[source]
        contract=WriterContract.daily_adapter(provider=provider,endpoint=endpoint,
            asset_type='etf' if source=='eastmoney_etf' else 'stock',
            instrument_family='sh_etf' if source=='eastmoney_etf' else 'sh_main',definition={'name':'raw'},
            evidence=b'SYNTHETIC SDK field declaration',publication_timezone='Asia/Shanghai')
        writer=FactWriter(writer.engine,contracts=[contract],clock=clock)
        request['contract_version']=contract.version
    if source=='sina':
        request['wire_record']={'open':'1','high':'1.1','low':'1','close':'1.05','volume':'1000','amount':None,'turnover':'0.02'}
    elif source=='tencent':
        request['wire_record']={'open':'1','high':'1.1','low':'1','close':'1.05','amount':'10'}
    raw=capture(contract,[row(request)],symbol)
    result=DailyBarSyncService(writer).ingest(raw,**args(request))
    fact=writer.fact(result['results'][0]['data_version'])
    assert fact['payload']['volume_shares']=='1000.00000000'
    assert fact['available_at'] is None and result['production_admission']=='not_certified'
    assert fact['source_tag']==result['source_tag']
    assert writer.evidence(fact['raw_hash'])['raw_bytes']==raw
    assert writer.current('daily_bars',fact['logical_key']) is None
    assert result['missing_dates']==['2026-06-02'] and not result['coverage_complete']
    if source=='sina':assert fact['payload']['turnover_rate_pct']=='2.0000000000'
    if source=='tencent':assert fact['payload']['amount_cny'] is None


def test_retries_and_empty_response_do_not_forge_coverage(system):
    writer,request,_,contract=system
    service=DailyBarSyncService(writer);raw=capture(contract,[row(request)])
    first=service.ingest(raw,**args(request));second=service.ingest(raw,**args(request))
    assert first['results']==second['results']
    audit=writer.audit(first['results'][0]['logical_key'])['counts']
    assert audit['facts']==1 and audit['observations']==2
    empty=service.ingest(capture(contract,[]),**args(request))
    assert empty['status']=='empty' and empty['missing_dates']==['2026-06-01','2026-06-02']
    assert not empty['coverage_complete']


def test_official_suspension_reference_is_staged_as_nontradable_without_filling_missing_days(system):
    writer,request,_,contract=system
    request['wire_record'].update({'开盘':'1.05','最高':'1.05','最低':'1.05','收盘':'1.05','成交量':'0','成交额':'0','换手率':'0'})
    proof={'status_bytes':canonical({'instrument_id':request['key']['instrument_id'],'trade_date':'2026-06-01',
        'is_suspended':True,'official_reference':'1.05'}).encode(),'status_source_uri':'https://www.sse.com.cn/synthetic-status',
        'official_reference':'1.05','previous_raw_close':None,'action_on_day':False}
    result=DailyBarSyncService(writer).ingest(capture(contract,[row(request)]),**args(request),suspension={'2026-06-01':proof})
    fact=writer.fact(result['results'][0]['data_version'])
    assert fact['payload']['is_suspended'] and fact['payload']['synthetic_bar']
    assert fact['payload']['volume_shares']=='0.00000000'
    assert result['missing_dates']==['2026-06-02'] and not result['coverage_complete']


def test_gap_plan_keeps_internal_holes_and_clips_lifecycle(system):
    writer,request,_,contract=system
    later=deepcopy(request);later['key']['trade_date']='2026-06-02'
    later['time'].update(event_date='2026-06-02',valid_from='2026-06-02',valid_to='2026-06-03')
    writer.write(later,approval(writer,later))
    planargs={k:v for k,v in args(request).items() if k in {'instrument_id','instrument_version','calendar_version','start','end_exclusive'}}
    service=DailyBarSyncService(writer)
    assert service.plan(**planargs)['request_dates']==['2026-06-01']
    from tests.test_governance_master_data import publish,member,code
    from app.services.governance_master_data import MasterDataService
    identity=request['key']['instrument_id']
    master=MasterDataService(writer.engine)
    version=publish(master,[member(identity,first='2026-06-02',last='2026-06-02',status='delisted')],
        [code(identity,'600001',first='2026-06-02',end='2026-06-03')],[],expected_head=request['instrument_version'])
    plan=service.plan(**(planargs|{'instrument_version':version,'end_exclusive':'2026-06-04'}))
    assert [r['status'] for r in plan['rows']]==['outside_lifecycle','covered','outside_lifecycle']


@pytest.mark.parametrize('bad', ['duplicate','qfq','provider','symbol','date','field','suspended_without_evidence','holiday','float'])
def test_invalid_capture_fails_before_any_write(system,bad):
    writer,request,_,contract=system
    rows=[row(request),row(request,'2026-06-02')]
    doc=__import__('json').loads(capture(contract,rows))
    if bad=='duplicate':doc['rows'][1]['trade_date']='2026-06-01'
    elif bad=='qfq':doc['response_basis']='qfq'
    elif bad=='provider':doc['provider']='tencent'
    elif bad=='symbol':doc['symbol']='sh600002'
    elif bad=='date':doc['rows'][1]['wire_record']['日期']='2026-06-01'
    elif bad=='field':doc['rows'][1]['wire_record']['extra']='1'
    elif bad=='suspended_without_evidence':doc['rows'][1]['wire_record']['is_suspended']=True
    elif bad=='holiday':doc['rows'][1]['trade_date']='2026-06-07'
    elif bad=='float':doc['rows'][1]['wire_record']['收盘']=1.05
    options=args(request)
    if bad=='holiday':options['end_exclusive']='2026-06-08'
    with pytest.raises(ValueError):DailyBarSyncService(writer).ingest(canonical(doc).encode(),**options)
    assert writer.audit(writer.preview(request)['logical_key'])['counts']['facts']==0


def test_batch_rollback_on_writer_failure(system,monkeypatch):
    writer,request,_,contract=system
    original=writer.write;calls=[]
    def fail_second(*a,**kw):
        calls.append(1)
        if len(calls)==2:raise RuntimeError('injected second write failure')
        return original(*a,**kw)
    monkeypatch.setattr(writer,'write',fail_second)
    with pytest.raises(RuntimeError):
        DailyBarSyncService(writer).ingest(capture(contract,[row(request),row(request,'2026-06-02')]),**args(request))
    assert writer.audit(writer.preview(request)['logical_key'])['counts']['facts']==0


def test_range_filter_and_explicit_revision_preserve_original(system):
    writer,request,clock,contract=system
    service=DailyBarSyncService(writer)
    first=service.ingest(capture(contract,[row(request)]),**args(request))['results'][0]
    original=writer.fact(first['data_version'])
    request['wire_record']['收盘']='1.04'
    correction={'kind':'correction','token':None,'order':None,'order_evidence':None,'supersedes_version':first['data_version']}
    clock.advance()
    result=service.ingest(capture(contract,[row(request),row(request,'2026-06-03')]),
        **args(request),revisions={'2026-06-01':correction})
    assert result['filtered_dates']==['2026-06-03']
    assert result['results'][0]['data_version']!=first['data_version']
    assert writer.fact(first['data_version'])==original


def test_tdx_without_verified_quantity_contract_is_blocked(system):
    writer,request,_,_=system
    from app.services.data_contract_profiles import get_profile
    fields={name:{'wire_field':name,'native_unit':'share' if name=='volume_shares' else 'CNY',
                  'normalized_unit':'share' if name=='volume_shares' else 'CNY','scale':'1','offset':'0'}
            for name in get_profile('daily_bars/core').required_fields}
    # An arbitrary caller-supplied unit claim is still insufficient for TDX.
    contract=WriterContract.profile(dataset='daily_bars',profile='daily_bars/core',provider='TDX',endpoint='TDX:day',
        asset_type='stock',instrument_family='sh_main',definition={'name':'unverified caller claim'},fields=fields,
        evidence=b'SYNTHETIC unit claim, not a supplier certificate',publication_timezone='Asia/Shanghai')
    writer=FactWriter(writer.engine,contracts=[contract])
    with pytest.raises(WriterError,match='TDX_QUANTITY_CONTRACT_UNVERIFIED'):
        DailyBarSyncService(writer).ingest(b'local day bytes',**(args(request)|{'contract_version':contract.version}))


def test_native_sdk_capture_requests_raw_and_rejects_already_normalized_frame(system):
    import pandas as pd
    from app.services.governance_daily_sync import capture_sdk_history
    writer,request,clock,_=system
    contract=WriterContract.daily_adapter(provider='eastmoney',endpoint='stock_zh_a_hist',asset_type='stock',
        instrument_family='sh_main',definition={'name':'raw'},evidence=b'SYNTHETIC source unit declaration',
        publication_timezone='Asia/Shanghai',sdk={'name':'synthetic-sdk','version':'test-only'},sdk_evidence=b'SYNTHETIC field types')
    frame=pd.DataFrame([request['wire_record']|{'日期':'2026-06-01'}]);calls=[]
    def supplier(**kwargs):calls.append(kwargs);return frame
    raw=capture_sdk_history(contract,symbol='sh600001',start='2026-06-01',end_exclusive='2026-06-03',fetcher=supplier)
    assert calls==[{'symbol':'600001','start_date':'20260601','end_date':'20260602','period':'daily','adjust':''}]
    writer=FactWriter(writer.engine,contracts=[contract],clock=clock)
    result=DailyBarSyncService(writer).ingest(raw,**(args(request)|{'contract_version':contract.version}))
    assert writer.fact(result['results'][0]['data_version'])['payload']['volume_shares']=='1000.00000000'
    frame.attrs['unit_normalization']={'endpoint':'stock_zh_a_hist'}
    with pytest.raises(WriterError,match='NORMALIZED_INPUT_FORBIDDEN'):
        capture_sdk_history(contract,symbol='sh600001',start='2026-06-01',end_exclusive='2026-06-03',fetcher=supplier)


@pytest.mark.parametrize('receipt', [{},{'actual_adjustment':'qfq','symbol':'sh600001','returned_mode':'qfqday'},
    {'actual_adjustment':'','symbol':'sh600002','returned_mode':'day'},
    {'actual_adjustment':'','symbol':'sh600001','returned_mode':'qfqday'},
    {'actual_adjustment':'','symbol':'sh600001','returned_mode':'day'}])
def test_tencent_capture_requires_actual_raw_response_mode(receipt):
    import pandas as pd
    from app.services.governance_daily_sync import capture_sdk_history
    contract=WriterContract.daily_adapter(provider='tencent',endpoint='stock_zh_a_hist_tx',asset_type='stock',
        instrument_family='sh_main',definition={'name':'raw'},evidence=b'SYNTHETIC unit declaration',
        publication_timezone='Asia/Shanghai',sdk={'name':'synthetic-sdk','version':'test-only'},sdk_evidence=b'SYNTHETIC schema')
    frame=pd.DataFrame([{'date':'2026-06-01','open':'1','high':'1.1','low':'1','close':'1','amount':'10'}])
    frame.attrs['history_response']=receipt
    opts=dict(symbol='sh600001',start='2026-06-01',end_exclusive='2026-06-03',fetcher=lambda **kw:frame)
    if receipt=={'actual_adjustment':'','symbol':'sh600001','returned_mode':'day'}:
        assert b'sdk-wire-records-v1' in capture_sdk_history(contract,**opts)
    else:
        with pytest.raises(WriterError):capture_sdk_history(contract,**opts)
