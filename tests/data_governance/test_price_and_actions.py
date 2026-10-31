"""M06.1 public raw receipt and frozen factor/derived-price seams."""
from copy import deepcopy
from datetime import date, datetime, timezone
import pytest
from tests.test_governance_writer import system, candidate, annual_page, approval, capture_request

pytestmark=pytest.mark.whitebox


def test_legacy_qfq_or_unknown_receipt_cannot_silently_enter_raw_facts(system):
    from app.services.governance_writer import WriterError
    writer,request,_,_=system
    request.pop('price_evidence',None)
    request['source_uri']='legacy://universe_daily_bars/600001:qfq'
    with pytest.raises(WriterError,match='RAW_PRICE_EVIDENCE_REQUIRED'):
        writer.write(request)


def test_verified_raw_values_and_original_receipt_are_retrievable_without_rewriting_bytes(system):
    writer,request,_,_=system
    result=writer.write(request)
    fact=writer.fact(result['data_version'])
    assert fact['payload']['raw_close']=='1.05000000'
    assert fact['payload']['volume_shares']=='1000.00000000'
    assert writer.evidence(fact['raw_hash'])['raw_bytes']==b'{"synthetic":"original supplier bytes"}'
    receipts=writer.price_evidence(result['data_version'])
    assert receipts[0]['document']['request_basis']==receipts[0]['document']['response_basis']=='raw'
    assert receipts[0]['evidence']==request['price_evidence']


def test_frozen_unity_factors_and_composite_derived_key_leave_raw_unchanged(system):
    from app.services.governance_prices import PriceService,install_price_schema
    writer,request,clock,_=system
    raw=writer.write(request,approval(writer,request))['data_version']
    original=writer.fact(raw)
    install_price_schema(writer.engine)
    prices=PriceService(writer)
    spec={'instrument_id':request['key']['instrument_id'],'instrument_version':request['instrument_version'],
        'calendar_version':request['calendar_version'],'base_date':'2026-06-02','as_of':'2026-06-02T10:00:00Z',
        'knowledge_mode':'system_known','algorithm_version':'r1-qfq-v1','raw_data_versions':[raw],'event_inputs':[]}
    factor=prices.freeze_factors(spec,evidence=b'SYNTHETIC explicit empty event membership, not certified event coverage')
    assert prices.factor_set(factor)['rows']==[{'trade_date':'2026-06-01','factor':'1.0000000000000000'},
                                              {'trade_date':'2026-06-02','factor':'1.0000000000000000'}]
    first=prices.derive(raw,factor)
    assert first['raw_data_version']==raw and first['adj_factor_version']==factor
    assert first['adj_close']=='1.05000000' and first['volume_shares']=='1000.00000000'
    assert prices.derive(raw,factor)==prices.adjusted(raw,factor)==first
    assert writer.fact(raw)==original


def test_changed_capture_bytes_are_observations_and_do_not_create_fake_decisions(system):
    from app.services.governance_fact_contracts import canonical,digest
    writer,request,clock,_=system
    first=writer.write(request)
    clock.advance()
    request['raw_response']=b'{"synthetic":"same normalized values, a second capture"}'
    capture_request(request)
    request['batch_id']='second-observation'
    second=writer.write(request)
    assert second==first
    assert writer.audit(first['logical_key'])['counts']['decisions']==1
    assert writer.audit(first['logical_key'])['counts']['observations']==2


@pytest.fixture
def prices(system):
    from app.services.governance_prices import PriceService,install_price_schema
    writer,request,clock,_=system
    request['wire_record'].update({'开盘':'10','最高':'10.2','最低':'9.8','收盘':'10','成交量':'1','成交额':'1000'})
    request['gate_results']['P02']='pass'  # G02: 1000 CNY / 100 shares = 10 CNY.
    raw=writer.write(request,approval(writer,request))['data_version']
    install_price_schema(writer.engine)
    spec={'instrument_id':request['key']['instrument_id'],'instrument_version':request['instrument_version'],
        'calendar_version':request['calendar_version'],'base_date':'2026-06-02','as_of':'2026-06-02T10:00:00Z',
        'knowledge_mode':'system_known','algorithm_version':'r1-qfq-v1','raw_data_versions':[raw],'event_inputs':[]}
    return PriceService(writer),spec,raw,request,clock


def action_dependency(prices,*,cash='0',stock='1',rights='0',rights_price=None,event_type='stock_dividend',parent=None,on=date(2026,6,2),event_id='fixture-action'):
    """SQL setup of a prevalidated immutable R3 event, not event-ingestion coverage.

    M06.2 owns acquiring/admitting these facts. This fixture supplies the consumer
    seam's frozen normalized event contract and approved availability explicitly.
    """
    from sqlalchemy import insert
    from app.db import governance_schema as s
    from app.services.governance_fact_contracts import canonical,digest,hash_object,logical_identity
    service,spec,raw,_,_=prices
    payload={'event_type':event_type,'cash_dividend':cash,'stock_ratio':stock,'rights_ratio':rights,'rights_price':rights_price}
    evidence=canonical({'SYNTHETIC_normalized_action':payload,'effective_date':on,'event_id':event_id}).encode()
    logical,key=logical_identity('corporate_action',{'instrument_id':spec['instrument_id'],'authority_event_id':event_id})
    contract_doc=canonical({'kind':'synthetic-prevalidated-r3-action','currency':'CNY','ratio':'x','fields':sorted(payload)})
    identity={'dataset':'corporate_action','logical_key':logical,'source_tag':'SSE:synthetic-action',
        'contract_version':digest(contract_doc.encode()),'definition_id':'a'*64,'source_revision_token':'content:'+digest(evidence),
        'normalized_payload_hash':digest(canonical(payload).encode()),'publication_evidence_hash':digest(evidence),'supersedes_version':parent}
    version=hash_object('fact-v1',identity);decision=hash_object('fixture-decision',{'version':version})
    now=datetime(2026,6,2,9)
    with service.engine.begin() as db:
        if parent is None:db.execute(insert(s.fact_keys).values(dataset='corporate_action',logical_key=logical,key_json=key))
        db.execute(insert(s.raw_evidence).values(raw_hash=digest(evidence),raw_bytes=evidence,source_uri='fixture://SYNTHETIC-action',observed_at=now))
        db.execute(insert(s.fact_versions).values(data_version=version,**identity,key_json=key,payload_json=canonical(payload),raw_hash=digest(evidence),
            field_sources={},event_date=on,published_at=now,publication_date=None,publication_precision='exact',public_available_at=now,
            observed_at=now,valid_from=on,valid_to=None,revision_kind='correction' if parent else 'original',
            source_revision_order=None,source_revision_evidence_hash=None))
        db.execute(insert(s.source_decisions).values(decision_id=decision,dataset='corporate_action',logical_key=logical,candidate_version=version,
            previous_version=parent,event='T09' if parent else 'T01',from_state='canonical' if parent else 'none',to_state='canonical',
            actor='SYNTHETIC-event-fixture',evidence_hash=digest(evidence),decided_at=now,expected_pointer_revision=1 if parent else 0,result='accepted'))
        db.execute(insert(s.version_availability).values(data_version=version,available_at=now,decision_id=decision))
    return version


def test_g02_adjustment_has_a_new_composite_key_and_preserves_raw_units(prices):
    service,spec,raw,_,_=prices
    event=action_dependency(prices)
    spec['event_inputs']=[{'data_version':event,'previous_raw_data_version':raw}]
    factor=service.freeze_factors(spec,evidence=b'SYNTHETIC explicit event membership')
    result=service.derive(raw,factor)
    assert result['adj_close']=='5.00000000'
    assert result['volume_shares']=='100.00000000' and result['amount_cny']=='1000.00000000'
    assert service.raw(raw)['payload']['raw_close']=='10.00000000'
    assert service.factor_set(factor)['event_version_ids']==[event]
    assert service.factor_set(factor)['input_manifest']['spec']['event_inputs'][0]['previous_raw_data_version']==raw


def test_candidate_writer_can_append_derived_data_while_reader_and_legacy_cannot(prices):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.services.governance_database_roles import candidate_roles
    from app.services.governance_write_boundary import install_boundary
    from app.services.governance_writer import FactWriter
    from app.services.governance_prices import PriceService
    service,spec,raw,request,clock=prices
    install_boundary(service.engine,instrument_version=spec['instrument_version'])
    with candidate_roles(service.engine) as roles:
        append=PriceService(FactWriter(roles['writer'],contracts=list(service.writer.contracts.values()),clock=clock))
        factor=append.freeze_factors(spec,evidence=b'SYNTHETIC role membership')
        assert append.derive(raw,factor)['adj_close']=='10.00000000'
        reader=PriceService(FactWriter(roles['reader'],contracts=[],clock=clock))
        assert reader.adjusted(raw,factor)['adj_close']=='10.00000000'
        for role in ('reader','legacy','writer'):
            with pytest.raises(DBAPIError):
                with roles[role].begin() as db:db.execute(text('UPDATE adjusted_bars SET adj_close=99'))
            with pytest.raises(DBAPIError):
                with roles[role].begin() as db:db.execute(text('DELETE FROM adjustment_set_inputs'))


def test_missing_raw_dependency_is_unavailable_without_qfq_backfill(prices):
    from app.services.governance_fact_contracts import WriterError
    service,spec,_,_,_=prices
    spec['raw_data_versions']=['f'*64]
    with pytest.raises(WriterError,match='ADJUSTMENT_UNAVAILABLE:DEPENDENCY_MISSING'):
        service.freeze_factors(spec,evidence=b'SYNTHETIC missing raw')


def test_a_raw_capture_cannot_be_reused_under_a_different_frozen_contract(system):
    from app.services.governance_writer import FactWriter,WriterContract,WriterError
    writer,request,clock,_=system
    different=WriterContract.daily_adapter(provider='eastmoney',endpoint='stock_zh_a_hist',asset_type='stock',instrument_family='sh_main',
        definition={'name':'another frozen definition'},evidence=b'SYNTHETIC new contract',publication_timezone='Asia/Shanghai')
    other=FactWriter(writer.engine,contracts=[different],clock=clock)
    request['contract_version']=different.version
    with pytest.raises(WriterError,match='RAW_PRICE_CONTRACT_MISMATCH'):
        other.write(request)


@pytest.mark.parametrize('case',['null','qfq_request','hfq_response','unknown','wrong_bytes','qfq_uri','malformed'])
def test_unproved_or_inconsistent_price_capture_is_rejected_before_fact_creation(system,case):
    import json
    from app.services.governance_fact_contracts import canonical,logical_identity,WriterError
    writer,request,_,_=system
    capture=json.loads(request['price_evidence'])
    if case=='null':request['price_evidence']=None
    elif case=='malformed':request['price_evidence']=b'{}'
    else:
        if case=='qfq_request':capture['request_basis']='qfq'
        if case=='hfq_response':capture['response_basis']='hfq'
        if case=='unknown':capture['response_basis']=None
        if case=='wrong_bytes':capture['raw_hash']='0'*64
        if case=='qfq_uri':request['source_uri']='legacy://600001:qfq'
        request['price_evidence']=canonical(capture).encode()
    with pytest.raises(WriterError,match='RAW_PRICE_'):writer.write(request)
    key,_=logical_identity('daily_bars',request['key'])
    assert writer.audit(key)['counts']['facts']==0


def test_action_correction_creates_a_new_factor_and_derived_key_without_rewriting_old_results(prices):
    service,spec,raw,_,_=prices
    first_event=action_dependency(prices)
    spec['event_inputs']=[{'data_version':first_event,'previous_raw_data_version':raw}]
    first=service.freeze_factors(spec,evidence=b'SYNTHETIC original membership')
    assert service.derive(raw,first)['adj_close']=='5.00000000'
    changed=action_dependency(prices,cash='4',stock='0',event_type='cash_dividend',parent=first_event)
    spec['event_inputs']=[{'data_version':changed,'previous_raw_data_version':raw}]
    second=service.freeze_factors(spec,evidence=b'SYNTHETIC revised membership')
    assert first!=second and service.derive(raw,second)['adj_close']=='6.00000000'
    assert service.adjusted(raw,first)['adj_close']=='5.00000000'
    assert service.raw(raw)['payload']['raw_close']=='10.00000000'


@pytest.mark.parametrize('changes',[{'event_type':'unrecognized'}, {'cash':None}, {'cash':'11'}, {'stock':'-1'},
    {'rights':'1','rights_price':None}, {'rights':'NaN'}, {'stock':True}])
def test_missing_or_invalid_action_parameters_never_guess_a_factor(prices,changes):
    from app.services.governance_fact_contracts import WriterError
    service,spec,raw,_,_=prices
    event=action_dependency(prices,**changes)
    spec['event_inputs']=[{'data_version':event,'previous_raw_data_version':raw}]
    with pytest.raises(WriterError,match='ADJUSTMENT_UNAVAILABLE'):
        service.freeze_factors(spec,evidence=b'SYNTHETIC invalid action input')


def test_concurrent_identical_factor_and_derived_requests_converge_on_one_frozen_identity(prices):
    from concurrent.futures import ThreadPoolExecutor
    service,spec,raw,_,_=prices
    with ThreadPoolExecutor(max_workers=3) as pool:
        factors=list(pool.map(lambda _:service.freeze_factors(spec,evidence=b'SYNTHETIC identical concurrent inputs'),range(3)))
    assert len(set(factors))==1
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows=list(pool.map(lambda _:service.derive(raw,factors[0]),range(3)))
    assert rows[0]==rows[1]==rows[2] and rows[0]['adj_close']=='10.00000000'


@pytest.mark.parametrize('table',['adjustment_sets','adjustment_factor_rows','adjustment_set_inputs'])
def test_interrupted_factor_freeze_leaves_no_partial_set_and_retries_with_same_identity(prices,table):
    from sqlalchemy import event
    from app.services.governance_fact_contracts import WriterError
    service,spec,_,_,_=prices
    evidence=b'SYNTHETIC atomic input manifest'
    planned=service.preview_factors(spec,evidence=evidence)['adj_factor_version']
    def interrupt(conn,cursor,statement,parameters,context,many):
        if statement.startswith('INSERT INTO '+table+' '):raise RuntimeError('SYNTHETIC interrupted factor transaction')
    event.listen(service.engine,'after_cursor_execute',interrupt)
    try:
        with pytest.raises(RuntimeError,match='interrupted'):service.freeze_factors(spec,evidence=evidence)
    finally:event.remove(service.engine,'after_cursor_execute',interrupt)
    with pytest.raises(WriterError,match='FACTOR_SET_MISSING'):service.factor_set(planned)
    assert service.freeze_factors(spec,evidence=evidence)==planned


def test_database_cannot_silently_reduce_factor_precision_or_commit_an_unreadable_set(prices):
    from app.services.governance_fact_contracts import WriterError
    service,spec,raw,_,_=prices
    event=action_dependency(prices,stock='0',rights='1',rights_price='12',event_type='rights_issue')
    spec['event_inputs']=[{'data_version':event,'previous_raw_data_version':raw}]
    evidence=b'SYNTHETIC exact 1.1 factor; SQLite NUMERIC cannot represent scale16'
    planned=service.preview_factors(spec,evidence=evidence)
    assert planned['rows'][0]['factor']=='1.1000000000000000'
    if service.engine.dialect.name=='sqlite':
        with pytest.raises(WriterError,match='ADJUSTMENT_UNAVAILABLE:STORAGE_PRECISION'):
            service.freeze_factors(spec,evidence=evidence)
        with pytest.raises(WriterError,match='FACTOR_SET_MISSING'):
            service.factor_set(planned['adj_factor_version'])
    else:
        factor=service.freeze_factors(spec,evidence=evidence)
        assert service.factor_set(factor)['rows'][0]['factor']=='1.1000000000000000'
        assert service.derive(raw,factor)['adj_close']=='11.00000000'


@pytest.mark.parametrize('case',['raw_id','event_id'])
def test_malformed_dependency_ids_fail_at_the_public_contract(prices,case):
    from app.services.governance_fact_contracts import WriterError
    service,spec,raw,_,_=prices
    if case=='raw_id':spec['raw_data_versions']=[{}]
    else:spec['event_inputs']=[{'data_version':[],'previous_raw_data_version':raw}]
    with pytest.raises(WriterError,match='FACTOR_INPUT_INVALID|EVENT_INPUT_INVALID'):
        service.preview_factors(spec,evidence=b'SYNTHETIC malformed request')


def test_event_products_are_not_rounded_per_event(prices):
    service,spec,raw,request,clock=prices
    first=action_dependency(prices,stock='2')  # r1 = 1/3.
    clock.now=datetime(2026,6,3,10,tzinfo=timezone.utc)
    later=deepcopy(request);later['key']['trade_date']='2026-06-02'
    later['time'].update(event_date='2026-06-02',valid_from='2026-06-02',valid_to='2026-06-03')
    later['raw_response']=b'SYNTHETIC June 2 raw capture';capture_request(later)
    previous=service.writer.write(later,approval(service.writer,later))['data_version']
    second=action_dependency(prices,stock='0',rights='1',rights_price='50',event_type='rights_issue',on=date(2026,6,3),event_id='fixture-action-2')  # r2 = 3.
    spec.update(base_date='2026-06-03',as_of='2026-06-03T10:00:00Z',raw_data_versions=[raw,previous],
        event_inputs=[{'data_version':first,'previous_raw_data_version':raw},{'data_version':second,'previous_raw_data_version':previous}])
    factor=service.freeze_factors(spec,evidence=b'SYNTHETIC explicit two-event membership')
    assert service.factor_set(factor)['rows']==[
        {'trade_date':'2026-06-01','factor':'1.0000000000000000'},
        {'trade_date':'2026-06-02','factor':'3.0000000000000000'},
        {'trade_date':'2026-06-03','factor':'1.0000000000000000'}]


@pytest.mark.parametrize('case',['before_raw','future_base','not_known','public_unknown','empty_raw','bad_mode','duplicate_event'])
def test_factor_request_never_substitutes_unknown_or_incompatible_dependencies(prices,case):
    from app.services.governance_fact_contracts import WriterError
    service,spec,raw,_,_=prices
    if case=='before_raw':spec['base_date']='2026-05-29'
    if case=='future_base':spec['base_date']='2026-06-03'
    if case=='not_known':spec['as_of']='2026-06-02T09:00:00Z'
    if case=='public_unknown':spec['knowledge_mode']='public_known'
    if case=='empty_raw':spec['raw_data_versions']=[]
    if case=='bad_mode':spec['knowledge_mode']='latest'
    if case=='duplicate_event':
        event=action_dependency(prices)
        spec['event_inputs']=[{'data_version':event,'previous_raw_data_version':raw}]*2
    with pytest.raises(WriterError):service.freeze_factors(spec,evidence=b'SYNTHETIC invalid dependencies')


@pytest.mark.parametrize('phase',['before_cursor_execute','after_cursor_execute'])
def test_derived_price_transaction_failure_rolls_back_and_retries(prices,phase):
    from sqlalchemy import event
    from app.services.governance_fact_contracts import WriterError
    service,spec,raw,_,_=prices
    factor=service.freeze_factors(spec,evidence=b'SYNTHETIC derived transaction')
    def interrupt(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO adjusted_bars '):raise RuntimeError('SYNTHETIC failed derived write')
    event.listen(service.engine,phase,interrupt)
    try:
        with pytest.raises(RuntimeError,match='failed derived'):service.derive(raw,factor)
    finally:event.remove(service.engine,phase,interrupt)
    with pytest.raises(WriterError,match='DERIVED_MISSING'):service.adjusted(raw,factor)
    assert service.derive(raw,factor)['adj_close']=='10.00000000'


@pytest.mark.parametrize('case',['coefficient','header','manifest','evidence','derived'])
def test_pinned_factor_and_price_integrity_are_checked_on_every_read(prices,case):
    from sqlalchemy import update
    from app.db import governance_schema as s,governance_price_schema as p
    from app.services.governance_fact_contracts import WriterError
    service,spec,raw,_,_=prices
    factor=service.freeze_factors(spec,evidence=b'SYNTHETIC integrity membership')
    service.derive(raw,factor)
    original=service.factor_set(factor)
    with service.engine.begin() as db:
        if case=='coefficient':db.execute(update(s.adjustment_factor_rows).where(s.adjustment_factor_rows.c.adj_factor_version==factor).values(factor='0.75'))
        if case=='header':db.execute(update(s.adjustment_sets).where(s.adjustment_sets.c.adj_factor_version==factor).values(algorithm_version='tampered'))
        if case=='manifest':db.execute(update(p.inputs).where(p.inputs.c.adj_factor_version==factor).values(manifest_json='{}'))
        if case=='evidence':db.execute(update(s.raw_evidence).where(s.raw_evidence.c.raw_hash==original['input_manifest']['evidence_hash']).values(raw_bytes=b'SYNTHETIC tampered'))
        if case=='derived':db.execute(update(s.adjusted_bars).where(s.adjusted_bars.c.raw_data_version==raw).values(adj_close='9'))
    with pytest.raises(WriterError,match='FACTOR_SET_INTEGRITY_FAILED|DERIVED_PRICE_INTEGRITY_FAILED'):
        service.adjusted(raw,factor)


def test_caller_mutation_during_database_write_cannot_change_frozen_event_inputs(prices):
    from sqlalchemy import event
    service,spec,raw,_,_=prices
    dependency=action_dependency(prices)
    spec['event_inputs']=[{'data_version':dependency,'previous_raw_data_version':raw}]
    def mutate(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO adjustment_sets '):spec['event_inputs'][0]['previous_raw_data_version']='f'*64
    event.listen(service.engine,'before_cursor_execute',mutate)
    try:factor=service.freeze_factors(spec,evidence=b'SYNTHETIC frozen caller input')
    finally:event.remove(service.engine,'before_cursor_execute',mutate)
    assert service.factor_set(factor)['input_manifest']['spec']['event_inputs'][0]['previous_raw_data_version']==raw
    assert service.derive(raw,factor)['adj_close']=='5.00000000'


def test_raw_mode_proof_observed_later_cannot_certify_an_earlier_system_as_of(prices):
    from sqlalchemy import update
    from app.db import governance_schema as s
    from app.services.governance_fact_contracts import WriterError
    service,spec,raw,_,clock=prices
    receipt=service.writer.price_evidence(raw)[0]
    clock.now=datetime(2026,6,2,12,tzinfo=timezone.utc)
    # A mode receipt first obtained at 11:00 must not certify raw at 10:00.
    with service.engine.begin() as db:
        db.execute(update(s.raw_evidence).where(s.raw_evidence.c.raw_hash==receipt['evidence_hash']).values(observed_at=datetime(2026,6,2,11)))
    with pytest.raises(WriterError,match='ADJUSTMENT_UNAVAILABLE:DEPENDENCY_NOT_KNOWN'):
        service.freeze_factors(spec,evidence=b'SYNTHETIC earlier system cutoff')
