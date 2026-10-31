"""M05.3: public writer/inspection boundary on real candidate databases."""
from datetime import datetime, timezone, timedelta
from copy import deepcopy
import json
import pytest
from tests.test_governance_schema import candidate
from tests.test_governance_master_data import annual_page

pytestmark = pytest.mark.whitebox


class Clock:
    def __init__(self): self.now = datetime(2026, 6, 2, 10, tzinfo=timezone.utc)
    def __call__(self): return self.now
    def advance(self): self.now += timedelta(seconds=1)


def capture_request(request):
    """Explicit synthetic raw-mode receipt, never a live provider certification."""
    from app.services.governance_fact_contracts import canonical,digest
    request['price_evidence']=canonical({'request_basis':'raw','response_basis':'raw',
        'raw_hash':digest(request['raw_response']),'contract_version':request['contract_version']}).encode('utf-8')
    return request


@pytest.fixture
def system(candidate, annual_page):
    from app.db.governance_migration import migrate_candidate
    from app.services.governance_master_data import MasterDataService
    from app.services.governance_writer import FactWriter, WriterContract
    from tests.test_governance_master_data import publish, member, code
    migrate_candidate(candidate)
    master = MasterDataService(candidate)
    identity = master.register_identity('authority:security-A', asset_type='stock', exchange='SSE')
    instruments = publish(master, [member(identity)], [code(identity, '600001')], [])
    from app.services.trade_calendar_sync import SOURCE_URL
    calendar = master.publish_calendar(exchange='SSE',source_uri=SOURCE_URL,evidence=annual_page)
    clock = Clock()
    contract = WriterContract.daily_adapter(provider='eastmoney', endpoint='stock_zh_a_hist', asset_type='stock',
        instrument_family='sh_main', definition={'name':'raw-price-CNY/share','version':1},
        evidence=b'ISOLATED synthetic raw/unit contract proof', publication_timezone='Asia/Shanghai')
    writer = FactWriter(candidate, contracts=[contract], clock=clock)
    request = {'dataset':'daily_bars','key':{'instrument_id':identity,'trade_date':'2026-06-01'},
        'contract_version':contract.version,'profile':'daily_bars/core','instrument_version':instruments,'calendar_version':calendar,
        'wire_record':{'开盘':'1.000000005','最高':'1.1','最低':'1','收盘':'1.05','成交量':'10','成交额':None,'换手率':'0.1'},
        'raw_response':b'{"synthetic":"original supplier bytes"}','source_uri':'fixture://supplier/raw',
        'time':{'event_date':'2026-06-01','publication_precision':'unknown','published_at':None,'publication_date':None,
                'valid_from':'2026-06-01','valid_to':'2026-06-02','publication_evidence':None},
        'revision':{'kind':'original','token':None,'order':None,'order_evidence':None,'supersedes_version':None},
        'flags':{'is_suspended':False,'synthetic_bar':False},'gate_results':{'P01':'pass'},'field_sources':{},'batch_id':'batch-A'}
    capture_request(request)
    return writer, request, clock, contract


def test_writer_stages_normalized_immutable_fact_without_granting_availability(system):
    writer, request, clock, contract = system
    result = writer.write(request)
    fact = writer.fact(result['data_version'])
    assert fact['payload']['raw_open'] == '1.00000001'
    assert fact['payload']['volume_shares'] == '1000.00000000'
    assert fact['payload']['amount_cny'] is None
    assert fact['available_at'] is None
    assert writer.current('daily_bars', result['logical_key']) is None
    assert result['accepted'] is False
    assert writer.audit(result['logical_key'])['counts'] == {'facts':1,'decisions':1,'receipts':1,'observations':1,'outbox':1,'history':0}


def approval(writer, request, revision=0, event='T01'):
    return {**{k:v for k,v in writer.preview(request).items() if k in ('dataset','logical_key','data_version')},
        'expected_pointer_revision':revision,'event':event,'to_state':'canonical','actor':'isolated:reviewer',
        'evidence':b'SYNTHETIC decision executor command; not a production approval'}


def test_exact_retry_returns_same_token_and_first_time_but_logs_new_observation(system):
    writer, request, clock, _ = system
    decision = approval(writer, request)
    first = writer.write(request, decision)
    original = writer.fact(first['data_version'])
    clock.advance()
    request['batch_id'] = 'retry-B'
    second = writer.write(request, decision)
    assert second == first and first['accepted'] is True
    assert writer.fact(first['data_version']) == original
    assert writer.current('daily_bars', first['logical_key'])['pointer_revision'] == 1
    assert writer.audit(first['logical_key'])['counts'] == {'facts':1,'decisions':1,'receipts':1,'observations':2,'outbox':1,'history':1}


def test_date_only_publication_is_conservative_and_future_approval_does_not_write(system):
    from app.services.governance_writer import WriterError
    writer, request, clock, _ = system
    request['time'].update(publication_precision='date', publication_date='2026-06-02', publication_evidence=b'source local publication date')
    preview = writer.preview(request)
    with pytest.raises(WriterError, match='NOT_YET_PUBLIC'):
        writer.write(request, approval(writer, request))
    assert writer.audit(preview['logical_key'])['counts']['facts'] == 0
    clock.now = datetime(2026, 6, 2, 16, tzinfo=timezone.utc)
    result = writer.write(request, approval(writer, request))
    fact = writer.fact(result['data_version'])
    assert fact['public_available_at'] == datetime(2026, 6, 2, 16)
    assert fact['published_at'] is None


@pytest.mark.parametrize('case', ['zero_id','legacy_id','extra_key','wrong_day','empty_interval','bad_flag','unknown_profile','float_wire','unknown_field'])
def test_invalid_identity_shape_or_contract_cannot_enter_version_store(system,case):
    from app.services.governance_writer import WriterError
    writer, request, _, _ = system
    if case=='zero_id': request['key']['instrument_id']=0
    elif case=='legacy_id': request['key']['instrument_id']=77
    elif case=='extra_key': request['key']['adjust']='qfq'
    elif case=='wrong_day': request['time']['event_date']='2026-06-02'
    elif case=='empty_interval': request['time']['valid_to']='2026-06-01'
    elif case=='bad_flag': request['flags']['is_suspended']=1
    elif case=='unknown_profile': request['profile']='financial/net_profit'
    elif case=='float_wire': request['wire_record']['开盘']=1.0
    elif case=='unknown_field': request['wire_record']['extra']=1
    with pytest.raises((WriterError,ValueError)):
        writer.write(request)


def test_independent_profit_profile_can_commit_without_core_and_preserves_frozen_units(system):
    from app.services.governance_writer import FactWriter, WriterContract
    writer, request, clock, _ = system
    contract=WriterContract.profile(dataset='financial',profile='financial/net_profit',provider='baostock',endpoint='profit_data',asset_type='stock',instrument_family='sh_main',
        definition={'scope':'consolidated','profit':'parent','revenue':'operating','period':'cumulative','roe':'weighted_period'},
        fields={'net_profit_parent':{'wire_field':'profit','native_unit':'CNY_10000','normalized_unit':'CNY','scale':'10000','offset':'0'}},
        evidence=b'SYNTHETIC unit proof',publication_timezone='Asia/Shanghai')
    writer=FactWriter(writer.engine,contracts=[contract],clock=clock)
    request.update(dataset='financial',contract_version=contract.version,profile='financial/net_profit',
        key={'instrument_id':request['key']['instrument_id'],'report_period':'2026-03-31','report_type':'q1','definition_id':contract.document['definition_id']},
        wire_record={'profit':'-12.5'},flags={},calendar_version=None,gate_results={})
    request['time'].update(event_date='2026-03-31',valid_from='2026-03-31',valid_to='2026-04-01')
    result=writer.write(request,approval(writer,request))
    assert writer.fact(result['data_version'])['payload']['net_profit_parent']=='-125000.00000000'
    assert result['accepted'] and result['request_quality_status']=='complete'
    assert writer.current('financial',result['logical_key'])['quality_status']=='partial'


def test_late_old_revision_is_audited_and_unordered_changed_content_becomes_conflict(system):
    from app.services.governance_writer import FactWriter, WriterContract
    writer, request, clock, _ = system
    contract=WriterContract.daily_adapter(provider='eastmoney',endpoint='stock_zh_a_hist',asset_type='stock',instrument_family='sh_main',
        definition={'name':'raw'},evidence=b'SYNTHETIC source counter ordering proof',publication_timezone='Asia/Shanghai',revision_order='integer')
    writer=FactWriter(writer.engine,contracts=[contract],clock=clock)
    request['contract_version']=contract.version
    capture_request(request)
    request['revision'].update(token='r8',order=8,order_evidence=b'counter8')
    first=writer.write(request,approval(writer,request))
    clock.advance()
    late=deepcopy(request)
    late['wire_record']['收盘']='1.02'
    late['revision'].update(kind='correction',token='r7',order=7,order_evidence=b'counter7',supersedes_version=first['data_version'])
    old=writer.write(late,approval(writer,late,1,'T09'))
    assert not old['accepted'] and old['outcome']=='ignored'
    assert writer.fact(old['data_version'])['available_at'] is None
    assert writer.current('daily_bars',first['logical_key'])['data_version']==first['data_version']
    ambiguous=deepcopy(late)
    ambiguous['revision'].update(token=None,order=None,order_evidence=None)
    conflict=writer.write(ambiguous,approval(writer,ambiguous,1,'T09'))
    assert not conflict['accepted'] and conflict['outcome']=='conflict'
    assert writer.audit(first['logical_key'])['counts']=={'facts':3,'decisions':3,'receipts':3,'observations':3,'outbox':3,'history':1}


def test_forward_revision_closes_exact_previous_interval_and_preserves_old_fact(system):
    from app.services.governance_writer import FactWriter, WriterContract
    writer, request, clock, _ = system
    contract=WriterContract.daily_adapter(provider='eastmoney',endpoint='stock_zh_a_hist',asset_type='stock',instrument_family='sh_main',
        definition={'name':'ordered'},evidence=b'SYNTHETIC ordered contract',publication_timezone='Asia/Shanghai',revision_order='integer')
    writer=FactWriter(writer.engine,contracts=[contract],clock=clock)
    request['contract_version']=contract.version
    capture_request(request)
    request['revision'].update(token='r8',order=8,order_evidence=b'proof8')
    first=writer.write(request,approval(writer,request))
    old=writer.fact(first['data_version'])
    clock.advance()
    request['wire_record']['收盘']='1.04'
    request['revision'].update(kind='correction',token='r9',order=9,order_evidence=b'proof9',supersedes_version=first['data_version'])
    second=writer.write(request,approval(writer,request,1,'T09'))
    assert second['accepted'] and second['pointer_revision']==2
    assert writer.fact(first['data_version'])==old
    rows=writer.audit(first['logical_key'])['history']
    assert len(rows)==2 and rows[0]['system_to']==rows[1]['system_from'] and rows[1]['system_to'] is None


def test_same_key_concurrent_identical_commands_share_token_and_distinct_commands_cas(system):
    from concurrent.futures import ThreadPoolExecutor
    from app.services.governance_writer import WriterError
    writer, request, clock, _=system
    command=approval(writer,request)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:writer.write(deepcopy(request),deepcopy(command)),range(3)))
    assert len({r['write_token'] for r in results})==1
    assert writer.audit(results[0]['logical_key'])['counts']['observations']==3
    # A different candidate may not use an old expected pointer revision.
    changed=deepcopy(request);changed['wire_record']['收盘']='1.03'
    with pytest.raises(WriterError,match='POINTER_STALE'):
        writer.write(changed,approval(writer,changed))
    assert writer.audit(results[0]['logical_key'])['counts']['facts']==1


@pytest.mark.parametrize('phase',['fact_versions','source_decisions','version_availability','fact_current','acceptance_history','write_receipts','governance_outbox','outbox_members'])
def test_database_failure_rolls_back_every_fact_pointer_audit_and_token(system,phase):
    from sqlalchemy import event
    writer, request, _, _=system
    preview=writer.preview(request);command=approval(writer,request)
    def fail(connection,cursor,statement,parameters,context,many):
        if statement.lstrip().lower().startswith('insert into '+phase+' '):
            raise RuntimeError('synthetic DB boundary failure '+phase)
    event.listen(writer.engine,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError,match='synthetic DB boundary'):
            writer.write(request,command)
    finally: event.remove(writer.engine,'before_cursor_execute',fail)
    assert writer.current('daily_bars',preview['logical_key']) is None
    assert writer.audit(preview['logical_key'])['counts']==dict.fromkeys(('facts','decisions','receipts','observations','outbox','history'),0)
    # An identical retry after rollback gets one complete committed transaction.
    assert writer.write(request,command)['accepted']


def test_malformed_or_naive_publication_time_cannot_be_made_available(system):
    from app.services.governance_writer import WriterError
    writer, request, _, _=system
    request['time'].update(publication_precision='exact',published_at='2026-06-01T15:00:00',publication_evidence=b'time')
    with pytest.raises(WriterError,match='TIME_OFFSET_REQUIRED'): writer.write(request)


def test_supplied_fake_global_pass_cannot_override_closed_calendar_or_failed_profile(system):
    from app.services.governance_writer import WriterError
    writer, request, _, _=system
    request['gate_results'].update(identity='pass',time='pass',P01='fail')
    with pytest.raises(WriterError,match='QUALITY_BLOCKED'): writer.write(request,approval(writer,request))
    assert writer.audit(writer.preview(request)['logical_key'])['counts']['facts']==0


@pytest.mark.parametrize('change', ['time_shape','key_shape','field_sources','source_list','profile_type','publication_evidence'])
def test_malformed_envelopes_fail_with_typed_error_before_any_write(system,change):
    from app.services.governance_writer import WriterError
    writer,request,_,_=system
    key=writer.preview(request)['logical_key']
    if change=='time_shape': request['time']=None
    elif change=='key_shape': request['key']=[]
    elif change=='field_sources': request['field_sources']={'raw_close':'unverified:other'}
    elif change=='source_list': request['field_sources']=[]
    elif change=='profile_type': request['profile']=None
    else: request['time']['publication_evidence']='string instead of original bytes'
    with pytest.raises(WriterError): writer.write(request)
    assert not any(writer.audit(key)['counts'].values())


def test_stage_then_activate_preserves_first_observation_and_pins_outbox_watermark(system):
    writer,request,clock,contract=system
    stage=writer.write(request)
    original=writer.fact(stage['data_version'])
    clock.advance()
    result=writer.write(request,approval(writer,request))
    fact=writer.fact(result['data_version'])
    assert fact['observed_at']==original['observed_at']
    assert fact['available_at']==clock.now.replace(tzinfo=None)
    audit=writer.audit(result['logical_key'])
    assert audit['counts']=={'facts':1,'decisions':2,'receipts':2,'observations':2,'outbox':2,'history':1}
    outbox=next(o for o in audit['outbox'] if o['write_token']==result['write_token'])
    assert outbox['version_ids']==[result['data_version']] and outbox['state']=='pending'
    watermark=outbox['source_high_water'][fact['source_tag']][result['logical_key']]
    assert watermark=={'data_version':result['data_version'],'source_revision_token':fact['source_revision_token'],'source_revision_order':None}


@pytest.mark.parametrize('provider', ['Fuyao','iFinD','BSE','Tushare','unregistered-vendor'])
def test_planned_or_excluded_brands_cannot_be_registered_as_generic_fact_contracts(provider):
    from app.services.governance_writer import WriterContract,WriterError
    with pytest.raises(WriterError,match='SOURCE_OUT_OF_RELEASE'):
        WriterContract.profile(dataset='financial',profile='financial/net_profit',provider=provider,endpoint='synthetic-unverified',asset_type='stock',instrument_family='sh_main',
            definition={'scope':'consolidated'},fields={'net_profit_parent':{'wire_field':'profit','native_unit':'CNY','normalized_unit':'CNY','scale':'1','offset':'0'}},
            evidence=b'SYNTHETIC cannot grant source access',publication_timezone='Asia/Shanghai')


def test_serialized_contract_cannot_forge_registered_daily_unit_mapping(system):
    from app.services.governance_writer import WriterContract,WriterError
    _,_,_,contract=system
    doc=contract.document
    doc['adapter']['fields'][0]['multiplier']='10000'
    with pytest.raises(WriterError,match='CONTRACT_DOCUMENT_INVALID'):
        WriterContract(json.dumps(doc,ensure_ascii=False),contract.evidence)


def test_corrupt_fact_is_detected_at_public_inspection_boundary(system):
    from sqlalchemy import update
    from app.db import governance_schema as s
    from app.services.governance_writer import WriterError
    writer,request,_,_=system
    result=writer.write(request)
    with writer.engine.begin() as db:
        db.execute(update(s.fact_versions).where(s.fact_versions.c.data_version==result['data_version']).values(payload_json='{}'))
    with pytest.raises(WriterError,match='HASH_COLLISION'): writer.fact(result['data_version'])


def test_raw_evidence_is_retrievable_and_cannot_be_rebound_to_other_bytes(system):
    from sqlalchemy import update
    from app.db import governance_schema as s
    from app.services.governance_writer import WriterError
    writer,request,clock,_=system
    result=writer.write(request)
    fact=writer.fact(result['data_version'])
    assert writer.evidence(fact['raw_hash'])['raw_bytes']==request['raw_response']
    with writer.engine.begin() as db:
        db.execute(update(s.raw_evidence).where(s.raw_evidence.c.raw_hash==fact['raw_hash']).values(raw_bytes=b'corrupt bytes'))
    with pytest.raises(WriterError,match='HASH_COLLISION'): writer.evidence(fact['raw_hash'])
    clock.advance()
    with pytest.raises(WriterError,match='HASH_COLLISION'): writer.write(request)
    assert writer.audit(result['logical_key'])['counts']['observations']==1


def test_two_distinct_simultaneous_candidates_only_one_cas_succeeds(system):
    from threading import Barrier
    from concurrent.futures import ThreadPoolExecutor
    from app.services.governance_writer import WriterError
    writer,request,_,_=system
    other=deepcopy(request);other['wire_record']['收盘']='1.03'
    barrier=Barrier(2)
    def send(req):
        command=approval(writer,req)
        barrier.wait(timeout=15)
        try: return writer.write(req,command)
        except WriterError as error: return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(send,[request,other]))
    assert results.count('POINTER_STALE')==1
    winner=next(r for r in results if isinstance(r,dict))
    assert winner['accepted'] and writer.current('daily_bars',winner['logical_key'])['data_version']==winner['data_version']
    assert writer.audit(winner['logical_key'])['counts']=={'facts':1,'decisions':1,'receipts':1,'observations':1,'outbox':1,'history':1}


def test_real_connection_loss_mid_transaction_has_no_half_write_and_retry_is_complete(system):
    from sqlalchemy import event,text
    from sqlalchemy.exc import DBAPIError
    writer,request,_,_=system
    command=approval(writer,request)
    hit=[]
    def disconnect(connection,cursor,statement,parameters,context,many):
        if not hit and statement.lstrip().lower().startswith('insert into write_receipts '):
            hit.append(True)
            if writer.engine.dialect.name=='mysql':
                assert writer.engine.url.host in {'127.0.0.1','localhost'} and writer.engine.url.database.startswith('codex_dsg_')
                identity=connection.scalar(text('SELECT CONNECTION_ID()'))
                with writer.engine.connect() as admin: admin.exec_driver_sql('KILL CONNECTION '+str(int(identity)))
            else: connection.connection.driver_connection.close()
    event.listen(writer.engine,'before_cursor_execute',disconnect)
    try:
        with pytest.raises(DBAPIError): writer.write(request,command)
    finally: event.remove(writer.engine,'before_cursor_execute',disconnect)
    assert hit and not any(writer.audit(command['logical_key'])['counts'].values())
    assert writer.current('daily_bars',command['logical_key']) is None
    assert writer.write(request,command)['accepted']


def test_date_objects_and_equivalent_public_offsets_have_same_fact_identity(system):
    from datetime import date
    writer,request,_,_=system
    request['time'].update(publication_precision='exact',published_at='2026-06-01T15:00:00+08:00',publication_evidence=b'exact original timestamp')
    expected=writer.preview(request)['data_version']
    request['key']['trade_date']=date(2026,6,1)
    for name in ('event_date','valid_from','valid_to'): request['time'][name]=date.fromisoformat(request['time'][name])
    request['time']['published_at']='2026-06-01T07:00:00Z'
    assert writer.preview(request)['data_version']==expected
    assert writer.write(request,approval(writer,request))['accepted']


def test_canonical_hash_literal_vector_normalizes_unicode_keys_and_rejects_collision():
    import hashlib
    from app.services.governance_fact_contracts import canonical,logical_identity,WriterError
    encoded='{"dataset":"daily_bars","instrument_id":101,"trade_date":"2026-06-01"}'
    key,text=logical_identity('daily_bars',{'trade_date':'2026-06-01','instrument_id':101})
    assert text==encoded and key==hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    assert canonical({'z':'e\u0301','a':'é'})=='{"a":"é","z":"é"}'
    with pytest.raises(WriterError,match='CANONICAL_KEY_COLLISION'): canonical({'é':1,'e\u0301':2})


def test_sdk_float_requires_bound_sdk_proof_and_matches_shortest_decimal_string(system):
    import hashlib
    from app.services.governance_writer import FactWriter,WriterContract
    writer,request,clock,_=system
    proof=b'SYNTHETIC SDK version and decoding evidence; not real supplier certification'
    contract=WriterContract.daily_adapter(provider='eastmoney',endpoint='stock_zh_a_hist',asset_type='stock',instrument_family='sh_main',definition={'name':'raw'},
        evidence=b'SYNTHETIC raw/unit evidence',publication_timezone='Asia/Shanghai',sdk={'name':'isolated-sdk','version':'test-1'},sdk_evidence=proof)
    writer=FactWriter(writer.engine,contracts=[contract],clock=clock)
    request['contract_version']=contract.version
    capture_request(request)
    request['wire_record']['开盘']=1.000000005
    first=writer.write(request)
    request['wire_record']['开盘']='1.000000005'
    assert writer.write(request)['write_token']==first['write_token']
    assert writer.fact(first['data_version'])['payload']['raw_open']=='1.00000001'
    assert writer.evidence(hashlib.sha256(proof).hexdigest())['raw_bytes']==proof


def test_activation_clock_cannot_precede_first_observation(system):
    from app.services.governance_writer import WriterError
    writer,request,clock,_=system
    stage=writer.write(request)
    clock.now-=timedelta(seconds=1)
    with pytest.raises(WriterError,match='CLOCK_NOT_MONOTONIC'): writer.write(request,approval(writer,request))
    assert writer.fact(stage['data_version'])['available_at'] is None


@pytest.mark.parametrize('parent_case',['missing','other_key','cycle'])
def test_invalid_or_cyclic_version_parent_cannot_write_a_candidate(system,parent_case):
    from sqlalchemy import update
    from app.db import governance_schema as s
    from app.services.governance_writer import WriterError
    writer,request,_,_=system
    original=writer.write(request)
    bad=deepcopy(request)
    bad['wire_record']['收盘']='1.03'
    parent=original['data_version']
    if parent_case=='missing': parent='f'*64
    elif parent_case=='other_key':
        bad['key']['trade_date']='2026-06-02'
        bad['time'].update(event_date='2026-06-02',valid_from='2026-06-02',valid_to='2026-06-03')
    else:
        with writer.engine.begin() as db:
            db.execute(update(s.fact_versions).where(s.fact_versions.c.data_version==parent).values(supersedes_version=parent))
    bad['revision'].update(kind='correction',supersedes_version=parent)
    with pytest.raises(WriterError,match='REVISION_PARENT_INVALID|VERSION_CYCLE'): writer.write(bad)
    assert writer.audit(original['logical_key'])['counts']['facts']==1
