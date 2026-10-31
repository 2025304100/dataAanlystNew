"""M07.5 index namespaces, native units and point-in-time composition inputs."""
from copy import deepcopy
from datetime import date
import base64,json
import pytest
from app.services.governance_index import IndexService,index_contract,install_index_schema,capture_index
from app.services.governance_writer import FactWriter
from app.services.governance_fact_contracts import canonical,WriterError,logical_identity
from tests.test_governance_writer import system,candidate,annual_page,approval

pytestmark=pytest.mark.whitebox


def setup(system,unit='lot'):
    base,template,clock,_=system;install_index_schema(base.engine)
    from app.services.governance_master_data import MasterDataService
    from tests.test_governance_master_data import publish,member,code
    master=MasterDataService(base.engine);first=template['key']['instrument_id']
    second=master.register_identity('authority:member-B',asset_type='stock',exchange='SSE')
    third=master.register_identity('authority:member-C',asset_type='stock',exchange='SSE')
    instruments=publish(master,[member(first),member(second),member(third)],
        [code(first,'600001'),code(second,'600002'),code(third,'600003')],[],expected_head=template['instrument_version'])
    contracts={p:index_contract(p,evidence=b'SYNTHETIC reviewed index-specific units, not live certification',volume_unit=unit) for p in ['eastmoney','sina','tencent']}
    writer=FactWriter(base.engine,contracts=list(contracts.values()),clock=clock);service=IndexService(writer)
    metadata={'index_id':'CSI:000300','authority':'CSI','quote_symbol':'000300','quote_market':'sh','eastmoney_secid':'1.000300',
        'calendar_exchange':'SSE','series_kind':'price','valid_from':'2020-01-01','valid_to':None,'expected_members':2}
    version=service.register_index(metadata,source_evidence=b'SYNTHETIC namespace authority record',review_evidence=b'SYNTHETIC explicit namespace review')
    return service,{'index_version':version,'calendar_version':template['calendar_version']},contracts,clock,instruments


def row(provider='eastmoney',on='2026-06-01'):
    if provider=='eastmoney':return {'日期':on,'开盘':'3000','收盘':'3050','最高':'3100','最低':'2900','成交量':'3',
        '成交额':'1000000000000','振幅':'0','涨跌幅':'0','涨跌额':'0','换手率':'0'}
    return {'date':on,'open':'3000','close':'3050','high':'3100','low':'2900',('quantity' if provider=='tencent' else 'volume'):'3'}


def archive(provider,version,rows,mode='day'):
    if provider=='eastmoney':
        names=['日期','开盘','收盘','最高','最低','成交量','成交额','振幅','涨跌幅','涨跌额','换手率']
        original=canonical({'rc':0,'data':{'market':1,'code':'000300','klines':[','.join(r[n] for n in names) for r in rows]}}).encode()
    elif provider=='tencent':original=canonical({'code':0,'data':{'sh000300':{mode:[[r[n] for n in ['date','open','close','high','low','quantity']] for r in rows]}}}).encode()
    else:original=None
    return canonical({'format':'index-records-v1','provider':provider,'index_version':version,'symbol':'sh000300',
        'basis':'unadjusted_index_points','rows':rows,'supplier_response_b64':base64.b64encode(original).decode() if original else None}).encode()


def quote(service,args,contracts,provider='eastmoney',rows=None,mode='day'):
    raw=archive(provider,args['index_version'],rows if rows is not None else [row(provider)],mode)
    options=dict(**args,contract_version=contracts[provider].version,start='2026-06-01',end_exclusive='2026-06-03',
        source_uri='fixture://native-index/'+provider,batch_id='A')
    return service.quote_requests(raw,**options)[0]


def composition(args,instruments,**kw):
    return {'index_version':args['index_version'],'instrument_version':instruments,'product':'constituents',
        'valid_from':'2026-05-01','valid_to':None,'publication_precision':'exact','published_at':'2026-04-30T16:00:00+08:00',
        'publication_date':None,'source_uri':'https://www.csindex.com.cn/synthetic-official-file','complete':True,
        'weight_unit':None,'constituents_version':None,'members':[{'exchange':'SSE','code':'600001'},{'exchange':'SSE','code':'600002'}],
        'revision_kind':'original','supersedes_version':None}|kw


def publish(service,doc,approve=True):return service.publish_composition(doc,source_evidence=canonical(doc).encode(),review_evidence=b'SYNTHETIC explicit source/effective-date review' if approve else None)


def read(service,args,**kw):return service.composition_as_of(index_version=args['index_version'],product='constituents',
    effective_date=kw.pop('effective_date','2026-05-20'),as_of=kw.pop('as_of','2026-06-02T10:00:00Z'),
    knowledge_mode=kw.pop('knowledge_mode','system_known'),**kw)


@pytest.mark.parametrize('provider',['eastmoney','sina','tencent'])
def test_index_points_native_quantity_and_amount_do_not_use_stock_vwap_or_cross_source_fill(system,provider):
    service,args,contracts,clock,inst=setup(system)
    request=quote(service,args,contracts,provider)
    result=service.writer.write(request,approval(service.writer,request))
    fact=service.writer.fact(result['data_version'])
    assert fact['payload']['index_close']=='3050.00000000' and fact['payload']['volume']=='3.00000000'
    assert fact['payload']['amount']==('1000000000000.00000000' if provider=='eastmoney' else None)
    assert fact['key']['index_id']=='CSI:000300' and 'instrument_id' not in fact['key']


def test_unknown_quantity_unit_remains_partial_even_with_spoofed_p12_pass(system):
    service,args,contracts,clock,inst=setup(system,'unknown')
    request=quote(service,args,contracts);request['gate_results']['P12']='pass'
    assert service.writer.preview(request)['request_quality_status']=='partial'
    result=service.writer.write(request)
    assert service.writer.fact(result['data_version'])['payload']['volume']=='3.00000000'
    with pytest.raises(WriterError,match='QUALITY_BLOCKED'):service.writer.write(request,approval(service.writer,request))


def test_same_point_definition_keeps_one_business_key_across_sources_without_filling_optional_cash(system):
    service,args,contracts,clock,inst=setup(system)
    requests=[quote(service,args,contracts,p) for p in ['eastmoney','sina','tencent']]
    assert len({r['key']['definition_id'] for r in requests})==1
    assert len({service.writer.preview(r)['logical_key'] for r in requests})==1
    results=[service.writer.write(r) for r in requests]
    assert len({r['data_version'] for r in results})==3
    payloads=[service.writer.fact(r['data_version'])['payload'] for r in results]
    assert payloads[0]['amount'] is not None and payloads[1]['amount'] is None and payloads[2]['amount'] is None


@pytest.mark.parametrize('bad',['qfq','fake_amount','wrong_index','missing_proof','date','duplicate','stock_namespace','hide_volume'])
def test_bad_index_identity_raw_mode_or_schema_fails_closed(system,bad):
    service,args,contracts,clock,inst=setup(system)
    if bad=='qfq':
        with pytest.raises(WriterError,match='RAW_MODE_UNVERIFIED'):quote(service,args,contracts,'tencent',mode='qfqday')
    elif bad=='fake_amount':
        r=row('tencent');r['amount']='1000'
        with pytest.raises(WriterError,match='UNEXPECTED_WIRE_FIELD'):quote(service,args,contracts,'tencent',[r])
    elif bad=='date':
        with pytest.raises(WriterError):quote(service,args,contracts,rows=[row(on='2026-06-03')])
    elif bad=='duplicate':
        with pytest.raises(WriterError,match='DUPLICATE_BUSINESS_KEY'):quote(service,args,contracts,rows=[row(),row()])
    else:
        request=quote(service,args,contracts)
        if bad=='missing_proof':request.pop('index_evidence')
        elif bad=='wrong_index':request['key']['index_id']='CSI:000905'
        elif bad=='hide_volume':request['wire_record']['volume']=None
        else:request['instrument_version']=inst
        with pytest.raises(ValueError):service.writer.write(request)


def test_future_effective_announcement_is_not_applied_before_effective_day_or_known_time(system):
    service,args,contracts,clock,inst=setup(system)
    old=publish(service,composition(args,inst))
    future=composition(args,inst,valid_from='2026-06-03',published_at='2026-05-10T16:00:00+08:00',
        members=[{'exchange':'SSE','code':'600001'},{'exchange':'SSE','code':'600003'}])
    new=publish(service,future)
    assert read(service,args,effective_date='2026-05-20',knowledge_mode='public_known',as_of='2026-05-15T00:00:00Z')['composition_version']==old
    assert read(service,args,effective_date='2026-06-03',knowledge_mode='public_known',as_of='2026-05-09T00:00:00Z')['composition_version']==old
    assert read(service,args,effective_date='2026-06-03',knowledge_mode='public_known',as_of='2026-05-15T00:00:00Z')['composition_version']==new
    assert read(service,args,effective_date='2026-05-20',as_of='2026-05-15T00:00:00Z')['status']=='missing'


def test_current_list_cannot_be_backdated_or_substitute_historical_constituents(system):
    service,args,contracts,clock,inst=setup(system)
    with pytest.raises(WriterError,match='CURRENT_COMPOSITION_NOT_HISTORICAL'):
        publish(service,composition(args,inst,product='current_constituents'))
    current=composition(args,inst,product='current_constituents',valid_from='2026-06-02',valid_to='2026-06-03')
    publish(service,current)
    assert read(service,args)['status']=='missing'
    result=service.composition_as_of(index_version=args['index_version'],product='current_constituents',effective_date='2026-06-02',
        as_of='2026-05-15T00:00:00Z',knowledge_mode='public_known')
    assert result['status']=='missing'


def test_date_only_publication_is_conservative_and_stage_does_not_grant_availability(system):
    service,args,contracts,clock,inst=setup(system)
    doc=composition(args,inst,publication_precision='date',published_at=None,publication_date='2026-05-01')
    staged=publish(service,doc,False)
    assert service.composition(staged)['available_at'] is None and read(service,args)['status']=='missing'
    approved=publish(service,doc)
    assert approved==staged
    assert read(service,args,knowledge_mode='public_known',as_of='2026-05-01T15:59:59Z')['status']=='missing'
    assert read(service,args,knowledge_mode='public_known',as_of='2026-05-01T16:00:00Z')['composition_version']==approved


@pytest.mark.parametrize('bad',['ratio','unknown','negative','sum','wrong_unit','wrong_members','duplicate'])
def test_weight_units_full_scope_binding_and_sum(system,bad):
    service,args,contracts,clock,inst=setup(system)
    constituents=publish(service,composition(args,inst))
    doc=composition(args,inst,product='weights',weight_unit='ratio' if bad=='ratio' else 'unknown' if bad=='unknown' else 'percent',
        constituents_version=constituents,members=[{'exchange':'SSE','code':'600001','weight':'0.6' if bad=='ratio' else '60'},
        {'exchange':'SSE','code':'600002','weight':'0.4' if bad=='ratio' else '40'}])
    if bad=='negative':doc['members'][0]['weight']='-1'
    elif bad=='sum':doc['members'][0]['weight']='65'
    elif bad=='wrong_unit':doc['weight_unit']='CNY'
    elif bad=='wrong_members':doc['members'][1]['code']='600003'
    elif bad=='duplicate':doc['members'][1]['code']='600001'
    if bad in {'ratio','unknown'}:
        version=publish(service,doc)
        result=service.composition_as_of(index_version=args['index_version'],product='weights',effective_date='2026-05-20',as_of=clock().isoformat(),knowledge_mode='system_known')
        assert result['status']==('pass' if bad=='ratio' else 'QUALITY_BLOCKED')
        assert result['members'][0]['weight_pct']==('60.0000000000' if bad=='ratio' else None)
        assert service.composition(version)['document']['constituents_version']==constituents
    else:
        with pytest.raises(ValueError):publish(service,doc)


def test_correction_and_withdrawal_preserve_old_asof_and_block_stale_weight_dependency(system):
    service,args,contracts,clock,inst=setup(system)
    original=composition(args,inst);first=publish(service,original);before=deepcopy(service.composition(first)['members'])
    weights=composition(args,inst,product='weights',weight_unit='percent',constituents_version=first,
        members=[{'exchange':'SSE','code':'600001','weight':'60'},{'exchange':'SSE','code':'600002','weight':'40'}])
    publish(service,weights);clock.advance()
    correction=composition(args,inst,revision_kind='correction',supersedes_version=first,published_at='2026-05-05T16:00:00+08:00',
        members=[{'exchange':'SSE','code':'600001'},{'exchange':'SSE','code':'600003'}])
    second=publish(service,correction);clock.advance()
    assert read(service,args,as_of='2026-06-02T10:00:00Z')['composition_version']==first
    assert read(service,args,as_of=clock().isoformat())['composition_version']==second
    assert read(service,args,knowledge_mode='public_known',as_of='2026-05-04T00:00:00Z')['composition_version']==first
    assert read(service,args,knowledge_mode='public_known',as_of='2026-05-06T00:00:00Z')['composition_version']==second
    result=service.composition_as_of(index_version=args['index_version'],product='weights',effective_date='2026-05-20',as_of=clock().isoformat(),knowledge_mode='system_known')
    assert result['status']=='DEPENDENCY_BLOCKED' and service.composition(first)['members']==before
    withdrawal=composition(args,inst,revision_kind='withdrawal',supersedes_version=second,published_at='2026-05-07T16:00:00+08:00',members=[])
    publish(service,withdrawal)
    assert read(service,args,as_of=clock().isoformat())['status']=='withdrawn'


def test_authorities_are_separate_and_invalid_members_or_parent_cannot_replace_history(system):
    service,args,contracts,clock,inst=setup(system)
    first=publish(service,composition(args,inst));clock.advance()
    with pytest.raises(WriterError,match='PARENT_NOT_CURRENT'):
        publish(service,composition(args,inst,members=[{'exchange':'SSE','code':'600001'}]))
    with pytest.raises(WriterError,match='AUTHORITY_MISMATCH'):
        publish(service,composition(args,inst,source_uri='https://www.cnindex.com.cn/file'))
    with pytest.raises(ValueError):publish(service,composition(args,inst,members=[{'exchange':'SSE','code':'699999'}]))
    assert read(service,args,as_of=clock().isoformat())['composition_version']==first


@pytest.mark.parametrize('part',['source','members'])
def test_source_archive_and_normalized_members_are_hash_checked(system,part):
    service,args,contracts,clock,inst=setup(system)
    version=publish(service,composition(args,inst))
    from app.db.governance_index_schema import compositions
    from sqlalchemy import select
    with service.writer.engine.begin() as db:
        if part=='source':values={'source_evidence':b'corrupt'}
        else:
            stored=json.loads(db.scalar(select(compositions.c.document_json).where(compositions.c.composition_version==version)))
            stored['members'][0]['instrument_id']+=999
            values={'document_json':canonical(stored)}
        db.execute(compositions.update().where(compositions.c.composition_version==version).values(**values))
    with pytest.raises(WriterError,match='COMPOSITION_HASH_MISMATCH'):service.composition(version)


@pytest.mark.parametrize('dataset',['index','etf'])
def test_changed_acquisition_bytes_only_add_observations_not_fake_decisions(system,dataset):
    if dataset=='index':
        service,args,contracts,clock,inst=setup(system)
        first=quote(service,args,contracts,'sina');changed=row('sina');changed['volume']='3.000'
        second=quote(service,args,contracts,'sina',[changed])
    else:
        from tests.data_governance.test_etf_profiles import setup as etf_setup,source
        service,args,clock,contracts,_=etf_setup(system)
        first=source(service,args,contracts,'nav','2');second=source(service,args,contracts,'nav','2.00000000')
    result=service.writer.write(first,approval(service.writer,first));before=service.writer.fact(result['data_version'])
    clock.advance();replay=service.writer.write(second,approval(service.writer,second))
    assert replay==result and service.writer.fact(result['data_version'])==before
    assert service.writer.audit(result['logical_key'])['counts']=={'facts':1,'decisions':1,'receipts':1,'observations':2,'outbox':1,'history':1}


def test_unknown_publication_and_future_publication_do_not_grant_early_public_knowledge(system):
    service,args,contracts,clock,inst=setup(system)
    unknown=composition(args,inst,publication_precision='unknown',published_at=None)
    publish(service,unknown)
    assert read(service,args)['status']=='pass'
    assert read(service,args,knowledge_mode='public_known')['status']=='PUBLICATION_UNKNOWN'
    future=composition(args,inst,valid_from='2026-07-01',published_at='2026-07-01T16:00:00+08:00')
    with pytest.raises(WriterError,match='NOT_YET_PUBLIC'):publish(service,future)


def test_cni_registry_and_official_composition_namespace_do_not_alias_csi(system):
    service,args,contracts,clock,inst=setup(system)
    doc=service.index(args['index_version'])|{'index_id':'CNI:399001','authority':'CNI','quote_symbol':'399001',
        'quote_market':'sz','calendar_exchange':'SZSE','eastmoney_secid':'0.399001'}
    version=service.register_index(doc,source_evidence=b'SYNTHETIC CNI authority',review_evidence=b'SYNTHETIC namespace review')
    source=composition(args,inst,index_version=version,source_uri='https://www.cnindex.com.cn/synthetic-official-file')
    snapshot=publish(service,source)
    assert service.composition_as_of(index_version=version,product='constituents',effective_date='2026-05-20',
        as_of=clock().isoformat(),knowledge_mode='system_known')['composition_version']==snapshot
    assert read(service,args)['status']=='missing'


def test_index_quote_batch_failure_rolls_back_and_migration_is_additive(system,monkeypatch):
    service,args,contracts,clock,inst=setup(system)
    raw=archive('eastmoney',args['index_version'],[row(),row(on='2026-06-02')]);options=dict(**args,
        contract_version=contracts['eastmoney'].version,start='2026-06-01',end_exclusive='2026-06-03',source_uri='fixture://index',batch_id='A')
    first=service.quote_requests(raw,**options)[0];original=service.writer.write;calls=[]
    def fail(*a,**kw):
        calls.append(True)
        if len(calls)==2:raise RuntimeError('injected second row failure')
        return original(*a,**kw)
    monkeypatch.setattr(service.writer,'write',fail)
    with pytest.raises(RuntimeError):service.ingest_quotes(raw,**options)
    assert service.writer.audit(logical_identity('index',first['key'])[0])['counts']['facts']==0
    assert install_index_schema(service.writer.engine)['created_tables']==[]


@pytest.mark.parametrize('provider',['eastmoney','sina','tencent'])
def test_actual_capture_boundaries_use_registered_symbols_and_preserve_real_raw_mode(system,monkeypatch,provider):
    service,args,contracts,clock,inst=setup(system)
    import pandas as pd
    from types import SimpleNamespace
    calls=[]
    if provider=='sina':monkeypatch.setattr('app.services.akshare_utils.call_akshare_with_retry',lambda *a,**kw:pd.DataFrame([row('sina')]))
    else:
        raw=json.loads(archive(provider,args['index_version'],[row(provider)]));payload=base64.b64decode(raw['supplier_response_b64'])
        def get(*a,**kw):calls.append((a,kw));return SimpleNamespace(status_code=200,content=payload,raise_for_status=lambda:None)
        monkeypatch.setattr('requests.get',get)
    capture=capture_index(service,None,provider=provider,**{'index_version':args['index_version']},start='2026-06-01',end_exclusive='2026-06-03')
    doc=json.loads(capture);assert doc['symbol']=='sh000300' and len(doc['rows'])==1
    if provider=='eastmoney':assert calls[0][1]['params']['beg']=='20260601' and calls[0][1]['params']['end']=='20260602'
    if provider=='tencent':assert calls[0][1]['params']['param']=='sh000300,day,2026-06-01,2026-06-02,640,'
