"""M07.4 candidate source separation, immutable dependencies and P09-P11."""
from copy import deepcopy
from datetime import datetime,timezone,date
from decimal import Decimal,localcontext
import json,math
import pytest
from app.services.governance_etf import ETFService,etf_contract,install_etf_schema,capture_etf
from app.services.governance_fact_contracts import canonical,WriterError,WriterContract,digest
from app.services.governance_writer import FactWriter
from tests.test_governance_writer import system,candidate,annual_page,approval,capture_request

pytestmark=pytest.mark.whitebox


def setup(system):
    base,template,clock,_=system
    install_etf_schema(base.engine)
    from app.services.governance_master_data import MasterDataService
    from tests.test_governance_master_data import publish,member,code
    master=MasterDataService(base.engine);identity=master.register_identity('authority:ETF-A',asset_type='etf',exchange='SSE')
    instruments=publish(master,[member(identity)],[code(identity,'510300')],[],expected_head=template['instrument_version'])
    contracts={p:etf_contract(p,evidence=b'SYNTHETIC explicit ETF unit/basis contract, not live supplier certification') for p in ['nav','shares','iopv','size','tracking']}
    raw=WriterContract.daily_adapter(provider='eastmoney',endpoint='fund_etf_hist_em',asset_type='etf',instrument_family='sh_etf',
        definition={'name':'raw-price-CNY/share','version':1},evidence=b'SYNTHETIC raw ETF unit declaration',publication_timezone='Asia/Shanghai')
    writer=FactWriter(base.engine,contracts=[*contracts.values(),raw],clock=clock)
    service=ETFService(writer)
    args={'instrument_id':identity,'instrument_version':instruments,'calendar_version':template['calendar_version']}
    return service,args,clock,contracts,(raw,template)


def source(service,args,contracts,product,value='2',on='2026-06-01',share_class='listed_units',**overrides):
    row={'valuation_date':on,'share_class':share_class,'basis':{'nav':'official_NAV','shares':'listed_units','iopv':'IOPV'}[product],
        'unit':'10000_fund_units' if product=='shares' else 'CNY','value':value}
    if product=='iopv':row['observation_at']=on+'T15:00:00+08:00'
    row.update(overrides)
    capture=canonical({'format':'etf-records-v1','product':product,'symbol':'sh510300','rows':[row]}).encode()
    return service.source_requests(capture,**args,product=product,contract_version=contracts[product].version,
        source_uri='https://www.sse.com.cn/synthetic-shares' if product=='shares' else 'fixture://supplier/ETF',
        batch_id='A',start='2026-06-01',end_exclusive='2026-06-03')[0]


def accept(service,request):return service.writer.write(request,approval(service.writer,request))['data_version']


def derive(service,args,contracts,product,**kwargs):
    return service.derive_request(product,**args,contract_version=contracts[product].version,valuation_date='2026-06-01',
        share_class='listed_units',as_of='2026-06-02T10:00:00Z',**kwargs)


def series(service,args,kind='dividend_reinvested',points=61):
    from app.services.governance_master_data import MasterDataService
    dates=MasterDataService(service.writer.engine).trading_days(args['calendar_version'],'SSE','2026-01-01','2026-06-02')[-61:]
    values=[]
    with localcontext() as ctx:
        ctx.prec=80;level=Decimal(100)
        for index,on in enumerate(dates):
            if index and kind=='dividend_reinvested':level*=Decimal('1.01') if index%2 else Decimal('0.99')
            values.append({'date':on.isoformat(),'value':str(level)})
    return {'kind':kind,'instrument_id':args['instrument_id'] if kind=='dividend_reinvested' else None,
        'instrument_version':args['instrument_version'] if kind=='dividend_reinvested' else None,
        'share_class':'listed_units' if kind=='dividend_reinvested' else None,'benchmark_id':'CSI300-TR',
        'calendar_version':args['calendar_version'],'exchange':'SSE','currency':'CNY','unit':'total_return_index','points':values[-points:]}


def register(service,doc):return service.register_returns(doc,source_evidence=b'SYNTHETIC actual total-return input archive',review_evidence=b'SYNTHETIC explicit reinvestment/benchmark total-return definition review')


def test_nav_iopv_and_share_multiplier_remain_independent(system):
    service,args,clock,contracts,_=setup(system)
    for product,value,field,expected in [('nav','2','nav_official','2.00000000'),('shares','3','total_shares','30000.00000000'),('iopv','2.1','iopv','2.10000000')]:
        request=source(service,args,contracts,product,value)
        result=service.writer.write(request);fact=service.writer.fact(result['data_version'])
        assert fact['payload'][field]==expected and fact['available_at'] is None
        assert fact['key']['profile']=='etf/'+product
    assert service.writer.fact(result['data_version'])['payload'].get('nav_official') is None


@pytest.mark.parametrize('product,changes',[('nav',{'basis':'IOPV'}),('shares',{'unit':'share'}),
    ('iopv',{'observation_at':'2026-06-02T19:00:00+08:00'}),('iopv',{'observation_at':'2026-05-31T15:00:00+08:00'}),
    ('nav',{'currency':'USD'})])
def test_source_basis_unit_and_time_mismatch_cannot_enter_candidate(system,product,changes):
    service,args,clock,contracts,_=setup(system)
    with pytest.raises(ValueError):source(service,args,contracts,product,**changes)


def test_cross_source_size_is_recomputed_and_has_composite_lineage(system):
    service,args,clock,contracts,_=setup(system)
    nav=accept(service,source(service,args,contracts,'nav','2'));shares=accept(service,source(service,args,contracts,'shares','3'))
    request=derive(service,args,contracts,'size',nav_data_version=nav,shares_data_version=shares)
    result=service.writer.write(request,approval(service.writer,request));fact=service.writer.fact(result['data_version'])
    assert fact['payload']['fund_size_cny']=='60000.00000000'
    assert fact['field_sources']['fund_size_cny']==[nav,shares]
    assert service.writer.current('etf',result['logical_key'])['provenance_mode']=='composite'
    assert service.writer.evidence(digest(request['etf_evidence']))['raw_bytes']==request['etf_evidence']


@pytest.mark.parametrize('bad',['date','class','iopv','unaccepted','not_known','tampered_value','tampered_lineage'])
def test_size_requires_real_same_date_same_class_known_versions_and_no_spoofed_gates(system,bad):
    service,args,clock,contracts,_=setup(system)
    nav_request=source(service,args,contracts,'iopv' if bad=='iopv' else 'nav')
    nav=service.writer.write(nav_request)['data_version'] if bad=='unaccepted' else accept(service,nav_request)
    shares=accept(service,source(service,args,contracts,'shares','3',on='2026-06-02' if bad=='date' else '2026-06-01',
        share_class='other' if bad=='class' else 'listed_units'))
    if bad=='not_known':
        with pytest.raises(WriterError,match='NOT_KNOWN_AS_OF'):
            service.derive_request('size',**args,contract_version=contracts['size'].version,valuation_date='2026-06-01',share_class='listed_units',
                as_of='2026-06-02T09:59:59Z',nav_data_version=nav,shares_data_version=shares)
    elif bad in {'tampered_value','tampered_lineage'}:
        request=derive(service,args,contracts,'size',nav_data_version=nav,shares_data_version=shares)
        request['gate_results']['P10']='pass'
        if bad=='tampered_value':request['wire_record']['fund_size_cny']='600000000'
        else:request['field_sources']['fund_size_cny']=['a'*64,shares]
        with pytest.raises(WriterError):service.writer.write(request)
    else:
        with pytest.raises(WriterError):derive(service,args,contracts,'size',nav_data_version=nav,shares_data_version=shares)


def test_missing_size_input_stays_null_and_caller_gate_pass_is_downgraded(system):
    service,args,clock,contracts,_=setup(system)
    request=derive(service,args,contracts,'size')
    request['gate_results']['P10']='pass'
    assert service.writer.preview(request)['request_quality_status']=='partial'
    result=service.writer.write(request)
    assert service.writer.fact(result['data_version'])['payload']['fund_size_cny'] is None
    with pytest.raises(WriterError,match='QUALITY_BLOCKED'):service.writer.write(request,approval(service.writer,request))


def test_tracking_uses_61_total_return_levels_60_common_returns_sample_std_and_252(system):
    service,args,clock,contracts,_=setup(system)
    nav=register(service,series(service,args));benchmark=register(service,series(service,args,'total_return'))
    request=derive(service,args,contracts,'tracking',nav_return_version=nav,benchmark_return_version=benchmark,benchmark_id='CSI300-TR')
    result=service.writer.write(request,approval(service.writer,request));payload=service.writer.fact(result['data_version'])['payload']
    expected=math.sqrt(60/59)*0.01*math.sqrt(252)*100
    assert abs(float(payload['tracking_error_pct'])-expected)<1e-8
    assert payload['window']==60 and payload['nav_return_version']==nav and payload['benchmark_return_version']==benchmark
    assert register(service,series(service,args))==nav


@pytest.mark.parametrize('bad',['missing_benchmark','short','gap','wrong_kind','wrong_benchmark','paired_benchmark','price_index','not_known'])
def test_tracking_missing_or_incompatible_series_never_produces_complete_te(system,bad):
    service,args,clock,contracts,_=setup(system)
    doc=series(service,args);benchmark_doc=series(service,args,'total_return')
    if bad=='price_index':
        doc['kind']='price_index'
        with pytest.raises(WriterError,match='TOTAL_RETURN_REQUIRED'):register(service,doc)
        return
    if bad=='short':doc['points']=doc['points'][1:]
    if bad=='gap':doc['points'].pop(20);benchmark_doc['points'].pop(20)
    if bad=='wrong_kind':doc=benchmark_doc
    if bad=='paired_benchmark':benchmark_doc['benchmark_id']='OTHER'
    nav=register(service,doc);benchmark=None if bad=='missing_benchmark' else register(service,benchmark_doc)
    if bad=='not_known':
        with pytest.raises(WriterError,match='NOT_KNOWN_AS_OF'):
            service.derive_request('tracking',**args,contract_version=contracts['tracking'].version,valuation_date='2026-06-01',
                share_class='listed_units',as_of='2026-06-02T09:59:59Z',nav_return_version=nav,benchmark_return_version=benchmark,benchmark_id='CSI300-TR')
    elif bad in {'wrong_kind','wrong_benchmark','paired_benchmark'}:
        with pytest.raises(WriterError):derive(service,args,contracts,'tracking',nav_return_version=nav,benchmark_return_version=benchmark,benchmark_id='OTHER' if bad in {'wrong_benchmark','paired_benchmark'} else 'CSI300-TR')
    else:
        request=derive(service,args,contracts,'tracking',nav_return_version=nav,benchmark_return_version=benchmark,benchmark_id='CSI300-TR')
        request['gate_results']['P11']='pass';result=service.writer.write(request)
        assert service.writer.fact(result['data_version'])['payload']['tracking_error_pct'] is None
        assert result['request_quality_status']=='partial'


def test_official_premium_uses_known_same_day_raw_close_and_never_iopv(system):
    service,args,clock,contracts,(raw_contract,template)=setup(system)
    nav=accept(service,source(service,args,contracts,'nav','2'))
    request=deepcopy(template);request.update(contract_version=raw_contract.version,instrument_version=args['instrument_version'])
    request['key']['instrument_id']=args['instrument_id']
    request['wire_record'].update({'开盘':'2.05','最高':'2.2','最低':'2','收盘':'2.1','成交额':'2100'})
    request['gate_results']['P02']='pass';capture_request(request)
    raw=accept(service,request)
    result=service.official_premium(nav,raw,as_of='2026-06-02T10:00:00Z')
    assert result['premium_official_pct']=='5.0000000000'
    with pytest.raises(WriterError):service.official_premium(nav,raw,as_of='2026-06-02T09:00:00Z')
    iopv=accept(service,source(service,args,contracts,'iopv'))
    with pytest.raises(WriterError):service.official_premium(iopv,raw,as_of='2026-06-02T10:00:00Z')


def test_missing_etf_evidence_cannot_be_bypassed_through_writer(system):
    service,args,clock,contracts,_=setup(system)
    request=source(service,args,contracts,'nav');request.pop('etf_evidence')
    with pytest.raises(WriterError,match='ETF_INPUT_EVIDENCE_REQUIRED'):service.writer.write(request)


@pytest.mark.parametrize('on,allowed',[('2026-05-31',True),('2026-06-03',False)])
def test_official_nav_preserves_non_exchange_valuation_day_but_rejects_future_date(system,on,allowed):
    service,args,clock,contracts,_=setup(system)
    row={'valuation_date':on,'share_class':'listed_units','basis':'official_NAV','unit':'CNY','value':'2'}
    raw=canonical({'format':'etf-records-v1','product':'nav','symbol':'sh510300','rows':[row]}).encode()
    options=dict(product='nav',contract_version=contracts['nav'].version,**args,source_uri='fixture://official-NAV',batch_id='A',
        start='2026-05-31',end_exclusive='2026-06-04')
    if allowed:
        request=service.source_requests(raw,**options)[0];version=service.writer.write(request)['data_version']
        assert service.writer.fact(version)['key']['valuation_date']==on
    else:
        with pytest.raises(WriterError,match='ETF_VALUATION_IN_FUTURE'):service.source_requests(raw,**options)


def test_corrected_nav_changes_new_size_without_overwriting_old_fact_or_result(system):
    service,args,clock,contracts,_=setup(system)
    nav=accept(service,source(service,args,contracts,'nav','2'));shares=accept(service,source(service,args,contracts,'shares','3'))
    original=derive(service,args,contracts,'size',nav_data_version=nav,shares_data_version=shares)
    old=accept(service,original);old_fact=service.writer.fact(old)
    clock.advance()
    correction=source(service,args,contracts,'nav','2.2')
    correction['revision'].update(kind='correction',supersedes_version=nav)
    updated=service.writer.write(correction,approval(service.writer,correction,1,'T11'))['data_version']
    new=service.derive_request('size',**args,contract_version=contracts['size'].version,valuation_date='2026-06-01',
        share_class='listed_units',as_of=clock().isoformat(),nav_data_version=updated,shares_data_version=shares)
    staged=service.writer.write(new)
    assert service.writer.fact(staged['data_version'])['payload']['fund_size_cny']=='66000.00000000'
    assert service.writer.fact(old)==old_fact
    assert service.writer.current('etf',staged['logical_key'])['data_version']==old


def test_source_batch_failure_rolls_back_and_empty_does_not_produce_a_fact(system,monkeypatch):
    service,args,clock,contracts,_=setup(system)
    row={'valuation_date':'2026-06-01','share_class':'listed_units','basis':'official_NAV','unit':'CNY','value':'2'}
    options=dict(product='nav',contract_version=contracts['nav'].version,**args,source_uri='fixture://NAV',batch_id='batch',
        start='2026-06-01',end_exclusive='2026-06-03')
    raw=canonical({'format':'etf-records-v1','product':'nav','symbol':'sh510300','rows':[row,row|{'valuation_date':'2026-06-02'}]}).encode()
    first=service.source_requests(raw,**options)[0];real=service.writer.write;calls=[]
    def fail_second(*a,**kw):
        calls.append(True)
        if len(calls)==2:raise RuntimeError('injected second write failure')
        return real(*a,**kw)
    monkeypatch.setattr(service.writer,'write',fail_second)
    with pytest.raises(RuntimeError):service.ingest(raw,**options)
    from app.services.governance_fact_contracts import logical_identity
    assert service.writer.audit(logical_identity('etf',first['key'])[0])['counts']['facts']==0
    empty=canonical({'format':'etf-records-v1','product':'nav','symbol':'sh510300','rows':[]}).encode()
    assert service.ingest(empty,**options)==[]


def test_registered_total_return_bytes_are_immutable_and_tampering_is_detected(system):
    service,args,clock,contracts,_=setup(system)
    nav=register(service,series(service,args));benchmark=register(service,series(service,args,'total_return'))
    from app.db.governance_etf_schema import return_inputs
    with service.writer.engine.begin() as db:
        db.execute(return_inputs.update().where(return_inputs.c.series_version==nav).values(source_evidence=b'corrupt'))
    with pytest.raises(WriterError,match='ETF_RETURN_HASH_MISMATCH'):
        derive(service,args,contracts,'tracking',nav_return_version=nav,benchmark_return_version=benchmark,benchmark_id='CSI300-TR')


@pytest.mark.parametrize('product',['nav','iopv'])
def test_existing_transport_capture_does_not_convert_iopv_or_cumulative_nav_to_official_nav(monkeypatch,product):
    import pandas as pd
    if product=='nav':monkeypatch.setattr('app.services.akshare_utils.call_akshare_with_retry',lambda *a,**kw:pd.DataFrame([{'净值日期':date(2026,6,1),'单位净值':2.0,'累计净值':9.0}]))
    else:monkeypatch.setattr('app.services.etf_provider.fetch_etf_quotes',lambda *a,**kw:{'510300':{'trade_date':date(2026,6,1),'updated_at':'2026-06-01T15:00:00+08:00','nav':2.1,'nav_basis':'iopv','total_shares':999}})
    raw=capture_etf(None,product=product,symbol='sh510300',start='2026-06-01',end_exclusive='2026-06-03',share_class='listed_units')
    row=json.loads(raw)['rows'][0]
    assert row['basis']==('official_NAV' if product=='nav' else 'IOPV')
    assert row['value']==('2.0' if product=='nav' else '2.1') and 'total_shares' not in row
