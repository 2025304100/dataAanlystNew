"""M07.2 public candidate APIs on isolated SQLite and MySQL 5.7."""
from copy import deepcopy
from datetime import datetime,timezone
from contextlib import contextmanager
import json
import pytest
from app.services.governance_fundamentals import FundamentalSyncService, fundamental_contract, capture_baostock_valuation
from app.services.governance_fact_contracts import canonical,WriterError,WriterContract
from app.services.governance_writer import FactWriter
from tests.test_governance_writer import system,candidate,annual_page,approval

pytestmark=pytest.mark.whitebox


def archive(product,rows):return canonical({'format':'fundamental-records-v1','product':product,'rows':rows}).encode()


def environment(system,product='eastmoney_financial',profile=None):
    base,template,clock,_=system
    contract=fundamental_contract(product,evidence=b'SYNTHETIC source field/unit evidence, not live certification',profile=profile)
    writer=FactWriter(base.engine,contracts=[contract],clock=clock)
    return FundamentalSyncService(writer),dict(product=product,contract_version=contract.version,
        instrument_id=template['key']['instrument_id'],instrument_version=template['instrument_version'],
        calendar_version=template['calendar_version'],start='2026-05-01' if 'financial' in product else '2026-06-01',
        end_exclusive='2026-06-03',source_uri='fixture://supplier/native-records',batch_id='batch-A',profile=profile),clock,contract


def financial(**kw):
    return {'SECUCODE':'600001.SH','REPORT_DATE':'2026-03-31','NOTICE_DATE':'2026-05-01',
        'PARENTNETPROFIT':'-1234.5','TOTALOPERATEREVE':'8000','PARENTNETPROFITTZ':'-4.2',
        'TOTALOPERATEREVETZ':'12.3','ROEJQ':'0.8'}|kw


def valuation(product='eastmoney_valuation',**kw):
    if product=='baostock_valuation':return {'date':'2026-06-01','code':'sh.600001','peTTM':'-12.3','pbMRQ':'1.2'}|kw
    return {'SECURITY_CODE':'600001','TRADE_DATE':'2026-06-01','PE_TTM':'-12.3','PB_MRQ':'1.2',
            'TOTAL_MARKET_CAP':'100000','NOTLIMITED_MARKETCAP_A':'80000'}|kw


def test_f10_total_revenue_never_fills_operating_revenue_and_independent_profit_is_readable(system):
    service,kwargs,clock,contract=environment(system,profile='financial/net_profit')
    raw=archive(kwargs['product'],[financial()]);request=service.requests(raw,**kwargs)[0]
    staged=service.ingest(raw,**kwargs)['results'][0]
    assert service.writer.fact(staged['data_version'])['available_at'] is None
    accepted=service.writer.write(request,approval(service.writer,request))
    assert accepted['accepted']
    read=dict(dataset='financial',key=request['key'],as_of='2026-06-02T11:00:00Z',knowledge_mode='system_known')
    result=service.read_as_of(**read,profile='financial/net_profit')
    assert result['status']=='pass' and result['quality_status']=='partial'
    assert result['payload']['net_profit_parent']=='-1234.50000000'
    assert result['payload']['roe_weighted_period_pct']=='0.8000000000'
    assert result['payload']['revenue_operating'] is None and result['payload']['revenue_yoy_pct'] is None
    assert service.read_as_of(**read,profile='financial/core')['status']=='QUALITY_BLOCKED'


@pytest.mark.parametrize('product',['eastmoney_valuation','baostock_valuation'])
def test_ttm_and_mrq_are_exactly_mapped_and_source_is_not_composed(system,product):
    service,kwargs,clock,contract=environment(system,product)
    request=service.requests(archive(product,[valuation(product)]),**kwargs)[0]
    result=service.ingest(archive(product,[valuation(product)]),**kwargs)
    fact=service.writer.fact(result['results'][0]['data_version'])
    assert fact['payload']['pe_ttm_x']=='-12.3000000000' and fact['payload']['pb_mrq_x']=='1.2000000000'
    assert fact['definition_id']==request['key']['definition_id']
    assert fact['source_tag'].startswith(product.split('_')[0]+':')
    if product=='baostock_valuation':assert 'total_market_cap_cny' not in fact['payload']


@pytest.mark.parametrize('change',[{'市盈率-动态':'10'},{'PE_DYNAMIC':'10'},{'pe_ttm_x':'10'},{'pb_mrq_x':'2'}])
def test_dynamic_pe_and_canonical_aliases_are_rejected_before_write(system,change):
    service,kwargs,_,_=environment(system,'eastmoney_valuation')
    good=archive(kwargs['product'],[valuation()]);key=service.requests(good,**kwargs)[0]['key']
    with pytest.raises(WriterError,match='UNEXPECTED_WIRE_FIELD'):
        service.ingest(archive(kwargs['product'],[valuation(**change)]),**kwargs)
    from app.services.governance_fact_contracts import logical_identity
    assert service.writer.audit(logical_identity('valuation',key)[0])['counts']['facts']==0


def test_missing_pe_is_partial_and_zero_is_preserved(system):
    service,kwargs,_,_=environment(system,'baostock_valuation')
    request=service.requests(archive(kwargs['product'],[valuation(kwargs['product'],peTTM='',pbMRQ='0')]),**kwargs)[0]
    staged=service.writer.write(request)
    payload=service.writer.fact(staged['data_version'])['payload']
    assert payload['pe_ttm_x'] is None and payload['pb_mrq_x']=='0.0000000000'
    with pytest.raises(WriterError,match='QUALITY_BLOCKED'):service.writer.write(request,approval(service.writer,request))


@pytest.mark.parametrize('bad',['wrong_symbol','wrong_notice','period','dynamic_revenue','duplicate','future_period','float'])
def test_invalid_report_batch_has_no_partial_writes(system,bad):
    service,kwargs,_,_=environment(system)
    rows=[financial()]
    if bad=='wrong_symbol':rows[0]['SECUCODE']='600002.SH'
    elif bad=='wrong_notice':rows[0]['NOTICE_DATE']='2026-06-03'
    elif bad=='period':rows[0]['REPORT_DATE']='2026-03-30'
    elif bad=='dynamic_revenue':rows[0]['unavailable_operating_revenue']='100'
    elif bad=='duplicate':rows.append(financial(PARENTNETPROFIT='200'))
    elif bad=='future_period':rows[0]['REPORT_DATE']='2026-06-30'
    elif bad=='float':rows[0]['PARENTNETPROFIT']=1.5
    with pytest.raises(ValueError):service.ingest(archive(kwargs['product'],rows),**kwargs)


def test_same_day_unordered_change_conflicts_then_explicit_review_changes_asof_without_overwrite(system):
    service,kwargs,clock,_=environment(system,profile='financial/net_profit')
    first_request=service.requests(archive(kwargs['product'],[financial()]),**kwargs)[0]
    first=service.writer.write(first_request,approval(service.writer,first_request));old=service.writer.fact(first['data_version'])
    clock.advance()
    revision={'kind':'correction','token':None,'order':None,'order_evidence':None,'supersedes_version':first['data_version']}
    second=service.requests(archive(kwargs['product'],[financial(PARENTNETPROFIT='4321')]),
        **kwargs,revisions={'2026-03-31':revision})[0]
    conflict=service.writer.write(second,approval(service.writer,second,1,'T09'))
    assert conflict['outcome']=='conflict' and not conflict['accepted']
    clock.advance();accepted=service.writer.write(second,approval(service.writer,second,1,'T11'))
    assert accepted['accepted']
    read=dict(dataset='financial',key=second['key'],profile='financial/net_profit')
    assert service.read_as_of(**read,as_of='2026-06-02T10:00:00Z',knowledge_mode='system_known')['data_version']==first['data_version']
    assert service.read_as_of(**read,as_of='2026-06-02T10:00:03Z',knowledge_mode='system_known')['data_version']==accepted['data_version']
    assert service.read_as_of(**read,as_of='2026-05-01T15:59:59Z',knowledge_mode='public_known')['status']=='missing'
    assert service.read_as_of(**read,as_of='2026-05-01T16:00:00Z',knowledge_mode='public_known')['data_version']==accepted['data_version']
    assert service.writer.fact(first['data_version'])==old


def test_explicit_withdrawal_removes_new_view_but_preserves_old_view(system):
    service,kwargs,clock,_=environment(system,profile='financial/net_profit')
    request=service.requests(archive(kwargs['product'],[financial()]),**kwargs)[0]
    first=service.writer.write(request,approval(service.writer,request));clock.advance()
    revision={'kind':'withdrawal','token':None,'order':None,'order_evidence':None,'supersedes_version':first['data_version']}
    request=service.requests(archive(kwargs['product'],[financial(NOTICE_DATE='2026-05-02')]),**kwargs,revisions={'2026-03-31':revision})[0]
    new=service.writer.write(request,approval(service.writer,request,1,'T11'))
    read=dict(dataset='financial',key=request['key'],profile='financial/net_profit',knowledge_mode='public_known')
    assert service.read_as_of(**read,as_of='2026-05-01T16:00:00Z')['data_version']==first['data_version']
    assert service.read_as_of(**read,as_of='2026-05-02T16:00:00Z')['status']=='withdrawn'
    assert service.writer.fact(new['data_version'])['revision_kind']=='withdrawal'


def test_empty_capture_is_not_complete_and_baostock_financial_definition_is_blocked(system):
    service,kwargs,_,_=environment(system)
    result=service.ingest(archive(kwargs['product'],[]),**kwargs)
    assert result['status']=='empty' and not result['coverage_complete']
    with pytest.raises(WriterError,match='BAOSTOCK_FINANCIAL_DEFINITION_UNVERIFIED'):
        fundamental_contract('baostock_financial',evidence=b'average ROE / net profit is not weighted ROE / parent profit')


def test_writer_failure_rolls_back_entire_valuation_batch(system,monkeypatch):
    service,kwargs,_,_=environment(system,'eastmoney_valuation')
    raw=archive(kwargs['product'],[valuation(),valuation(TRADE_DATE='2026-06-02')])
    request=service.requests(raw,**kwargs)[0];original=service.writer.write;calls=[]
    def fail_second(*a,**kw):
        calls.append(True)
        if len(calls)==2:raise RuntimeError('injected failure')
        return original(*a,**kw)
    monkeypatch.setattr(service.writer,'write',fail_second)
    with pytest.raises(RuntimeError):service.ingest(raw,**kwargs)
    from app.services.governance_fact_contracts import logical_identity
    assert service.writer.audit(logical_identity('valuation',request['key'])[0])['counts']['facts']==0


def test_contract_cannot_relabel_total_revenue_as_operating(system):
    service,kwargs,clock,contract=environment(system)
    doc=contract.document
    doc['fields']['revenue_operating']['wire_field']='TOTALOPERATEREVE'
    forged=WriterContract(canonical(doc),contract.evidence)
    writer=FactWriter(service.writer.engine,contracts=[forged],clock=clock)
    with pytest.raises(WriterError,match='FUNDAMENTAL_CONTRACT_MISMATCH'):
        FundamentalSyncService(writer).ingest(archive(kwargs['product'],[financial()]),**(kwargs|{'contract_version':forged.version}))


@pytest.mark.parametrize('product',['eastmoney_valuation','eastmoney_financial'])
def test_existing_eastmoney_transport_records_are_archived_without_financial_aliases(monkeypatch,product):
    from app.services.governance_fundamentals import capture_eastmoney_fundamentals
    from datetime import date
    calls=[]
    def fetch(*a,**kw):
        calls.append((a,kw))
        return [financial()] if product.endswith('financial') else [{'symbol':'600001','trade_date':date(2026,6,1),
            'pe_ttm':12.3,'pb':1.2,'total_market_cap':100.0,'circulating_market_cap':80.0}]
    target='app.services.financial_provider.fetch_financial_rows' if product.endswith('financial') else 'app.services.valuation_provider.fetch_valuation_rows'
    monkeypatch.setattr(target,fetch)
    raw=capture_eastmoney_fundamentals(None,product=product,symbol='600001.SH' if product.endswith('financial') else '600001',
        start='2026-06-01',end_exclusive='2026-06-03')
    doc=json.loads(raw)
    assert calls[0][0][-1]==date(2026,6,2)
    if product.endswith('financial'):assert doc['rows'][0]['TOTALOPERATEREVE']=='8000'
    else:assert doc['rows'][0]['PE_TTM']=='12.3' and 'pe_ttm' not in doc['rows'][0]


@pytest.mark.parametrize('error',['none','initial','late','schema'])
def test_baostock_new_valuation_query_is_bounded_and_session_is_closed(error):
    from types import SimpleNamespace
    calls=[];closed=[]
    class Cursor:
        error_code='7' if error=='initial' else '0'
        fields=['unexpected'] if error=='schema' else ['date','code','peTTM','pbMRQ']
        count=0
        def next(self):
            self.count+=1
            if self.count==2 and error=='late':self.error_code='7'
            return self.count==1
        def get_row_data(self):return ['2026-06-01','sh.600001','10','1']
    def query(*a,**kw):calls.append((a,kw));return Cursor()
    @contextmanager
    def session():
        try:yield SimpleNamespace(query_history_k_data_plus=query)
        finally:closed.append(True)
    options=dict(session_factory=session,symbol='sh.600001',start='2026-06-01',end_exclusive='2026-06-03')
    if error=='none':
        result=json.loads(capture_baostock_valuation(**options))
        assert result['product']=='baostock_valuation' and len(result['rows'])==1
    else:
        with pytest.raises(WriterError,match='BAOSTOCK_RESPONSE_INVALID'):capture_baostock_valuation(**options)
    assert closed==[True]
    assert calls[0][1]=={'start_date':'2026-06-01','end_date':'2026-06-02','frequency':'d','adjustflag':'3'}
