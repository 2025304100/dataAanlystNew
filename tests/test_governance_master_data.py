"""M05.1 public master service over a real, explicitly initialized test database."""
from datetime import date
import pytest
from sqlalchemy import create_engine
from tests.test_data_source_execution import http_supplier

pytestmark=pytest.mark.whitebox


@pytest.fixture
def master(tmp_path):
    from app.db.governance_master_schema import initialize_master_schema
    from app.services.governance_master_data import MasterDataService
    engine=create_engine('sqlite:///'+(tmp_path/'master.sqlite').as_posix())
    initialize_master_schema(engine)
    yield MasterDataService(engine)
    engine.dispose()


def test_identity_allocation_is_persistent_independent_of_legacy_ids(master):
    from app.services.governance_master_data import MasterDataService
    first=master.register_identity('reviewed:issuer-A',asset_type='stock',exchange='SSE')
    second=master.register_identity('reviewed:issuer-B',asset_type='stock',exchange='SSE')
    reopened=MasterDataService(master.engine)
    assert reopened.register_identity('reviewed:issuer-A',asset_type='stock',exchange='SSE')==first
    assert second>first>0
    assert reopened.identity(first)=={'instrument_id':first,'identity_key':'reviewed:issuer-A','asset_type':'stock','exchange':'SSE'}


def member(identity,first='2020-01-01',last=None,status='listed'):
    return {'instrument_id':identity,'list_date':first,'last_trade_date':last,'lifecycle_status':status}


def code(identity,value,first='2020-01-01',end=None):
    return {'instrument_id':identity,'exchange':'SSE','code':value,'valid_from':first,'valid_to':end}


def alias(identity,table,old_id,value,first='2020-01-01',end=None):
    return {**code(identity,value,first,end),'legacy_table':table,'legacy_id':old_id}


def publish(master,members,codes,aliases,expected_head=None,evidence=b'reviewed identity/lifecycle fixture'):
    return master.publish_instruments(members=members,codes=codes,aliases=aliases,evidence=evidence,expected_head=expected_head)


def test_two_legacy_namespaces_resolve_only_in_explicit_frozen_version(master):
    first=master.register_identity('reviewed:A',asset_type='stock',exchange='SSE')
    other=master.register_identity('reviewed:B',asset_type='stock',exchange='SSE')
    version=publish(master,[member(first),member(other)],[code(first,'600001'),code(other,'600002')],
        [alias(first,'symbols',77,'600001'),alias(first,'universe_symbols',88,'600001'),alias(other,'universe_symbols',77,'600002')])
    assert master.resolve_legacy(version,'symbols',77,date(2026,1,1))['instrument_id']==first
    assert master.resolve_legacy(version,'universe_symbols',88,date(2026,1,1))['instrument_id']==first
    assert master.resolve_legacy(version,'universe_symbols',77,date(2026,1,1))['instrument_id']==other
    assert master.resolve_code(version,'SSE','600002',date(2026,1,1))['instrument_id']==other


def test_code_reuse_has_two_identities_and_delisted_last_day_is_inclusive(master):
    from app.services.governance_master_data import MasterDataError
    first=master.register_identity('authority:old-company',asset_type='stock',exchange='SSE')
    second=master.register_identity('authority:new-company',asset_type='stock',exchange='SSE')
    old=publish(master,[member(first)],[code(first,'600001')],[alias(first,'symbols',77,'600001')])
    new=publish(master,[member(first,last='2026-01-10',status='delisted'),member(second,'2026-01-11')],
        [code(first,'600001',end='2026-01-11'),code(second,'600001','2026-01-11')],
        [alias(first,'symbols',77,'600001',end='2026-01-11'),alias(second,'universe_symbols',77,'600001','2026-01-11')],expected_head=old)
    assert master.resolve_code(new,'SSE','600001',date(2026,1,10))['instrument_id']==first
    assert master.resolve_code(new,'SSE','600001',date(2026,1,11))['instrument_id']==second
    assert master.resolve_legacy(old,'symbols',77,date(2026,1,11))['instrument_id']==first
    with pytest.raises(MasterDataError,match='OUTSIDE_LIFECYCLE'):
        master.validate_lifecycle(new,first,date(2026,1,11))


def test_unknown_lifecycle_cannot_publish_canonical_identity(master):
    from app.services.governance_master_data import MasterDataError
    identity=master.register_identity('authority:unknown-history',asset_type='stock',exchange='SSE')
    with pytest.raises(MasterDataError,match='LIFECYCLE_UNKNOWN'):
        publish(master,[member(identity,first=None)],[code(identity,'600001')],[])


@pytest.fixture
def annual_page():
    # Synthetic complete annual source shape, never a live exchange certificate.
    periods={'元旦':('1月1日','1月3日'),'春节':('2月15日','2月23日'),'清明节':('4月4日','4月6日'),
        '劳动节':('5月1日','5月5日'),'端午节':('6月19日','6月21日'),'中秋节':('9月25日','9月27日'),'国庆节':('10月1日','10月7日')}
    return ('<h2>2026年休市安排</h2><table>'+''.join(f'<tr><td>{name}</td><td>{a}至{b}休市</td></tr>' for name,(a,b) in periods.items())+'</table>').encode('utf-8')


def test_official_calendar_correction_is_pinned_and_unknown_dates_do_not_become_weekends(master,annual_page):
    from app.services.governance_master_data import MasterDataError
    from app.services.trade_calendar_sync import SOURCE_URL
    first=master.publish_calendar(exchange='SSE',source_uri=SOURCE_URL,evidence=annual_page)
    second=master.publish_calendar(exchange='SSE',source_uri=SOURCE_URL,evidence=annual_page.replace('10月7日'.encode(),'10月8日'.encode()))
    assert first!=second
    assert master.trading_days(first,'SSE',date(2026,10,1),date(2026,10,9))==[date(2026,10,8)]
    assert master.trading_days(second,'SSE',date(2026,10,1),date(2026,10,9))==[]
    assert master.publish_calendar(exchange='SSE',source_uri=SOURCE_URL,evidence=annual_page)==first
    with pytest.raises(MasterDataError,match='CALENDAR_RANGE_UNKNOWN'):
        master.trading_days(first,'SSE',date(2027,1,2),date(2027,1,3))
    with pytest.raises(MasterDataError,match='CALENDAR_EXCHANGE_MISMATCH'):
        master.trading_days(first,'SZSE',date(2026,10,1),date(2026,10,9))


def test_official_capture_uses_existing_controlled_transport_and_preserves_source_bytes(master,annual_page,tmp_path,monkeypatch,http_supplier):
    import requests
    from app.services import trade_calendar_sync as service
    from app.services.data_source_control import RequestController,RequestPolicy
    c=RequestController(tmp_path/'control.sqlite')
    c.set_provider('sse',RequestPolicy(max_attempts=1))
    c.set_endpoint('CAL:official','sse',RequestPolicy(max_attempts=1),transport='requests',allowed_hosts=('www.sse.com.cn',))
    c.start_task('calendar-task',lane='incremental',max_requests=1,timeout_s=30)
    url,state=http_supplier;state['body']=annual_page
    send=requests.adapters.HTTPAdapter.send
    def loopback(adapter,request,**kwargs):
        request=request.copy();request.url=url;kwargs['proxies']={}
        return send(adapter,request,**kwargs)
    monkeypatch.setattr(requests.adapters.HTTPAdapter,'send',loopback)
    with c.scope('calendar-task'):
        result=service.capture_cn_calendar_version(master)
    assert result['exchange']=='SSE' and result['source_uri']==service.SOURCE_URL
    assert master.trading_days(result['calendar_version'],'SSE',date(2026,10,7),date(2026,10,9))==[date(2026,10,8)]
    assert state['hits']==['/']
    assert c.statistics()['totals']['attempts']==1


def test_legacy_candidate_inspection_keeps_namespaces_and_does_not_guess_lifecycle(db_session):
    from app.models.symbol import Symbol
    from app.models.universe import UniverseSymbol
    from app.services.governance_master_data import inspect_legacy_candidates
    db_session.add_all([Symbol(id=77,symbol='600001',name='current name',asset_type='stock',market='SH'),
        UniverseSymbol(id=77,symbol='600001',name='current name',asset_type='stock',market='sh',region='cn',listed_at=date(2020,1,1))])
    db_session.commit()
    rows=inspect_legacy_candidates(db_session)
    assert [(r['legacy_table'],r['legacy_id'],r['code'],r['exchange'],r['mapping_status']) for r in rows]==[
        ('symbols',77,'600001','SSE','unreviewed'),('universe_symbols',77,'600001','SSE','unreviewed')]
    assert all(r['instrument_id'] is None and r['lifecycle_status']=='unknown' for r in rows)
    assert db_session.get(Symbol,77).listed_at is None


@pytest.mark.parametrize('malformed',['code_in_aliases','alias_in_codes'])
def test_mapping_shapes_are_strict_and_failed_publication_does_not_change_head(master,malformed):
    from app.services.governance_master_data import MasterDataError
    identity=master.register_identity('authority:A',asset_type='stock',exchange='SSE')
    old=publish(master,[member(identity)],[code(identity,'600001')],[alias(identity,'symbols',77,'600001')])
    with pytest.raises(MasterDataError,match='MASTER_FIELDS_INVALID'):
        publish(master,[member(identity)],
            [alias(identity,'symbols',77,'600001')] if malformed=='alias_in_codes' else [code(identity,'600001')],
            [code(identity,'600001')] if malformed=='code_in_aliases' else [],expected_head=old)
    assert master.resolve_legacy(old,'symbols',77,date(2026,1,1))['instrument_id']==identity
    new=publish(master,[member(identity)],[code(identity,'600002')],[],expected_head=old,evidence=b'new reviewed mapping')
    assert master.resolve_code(new,'SSE','600002',date(2026,1,1))['instrument_id']==identity


def test_replaying_an_old_snapshot_cannot_rewind_the_current_mapping(master):
    from app.services.governance_master_data import MasterDataError
    identity=master.register_identity('authority:A',asset_type='stock',exchange='SSE')
    first=publish(master,[member(identity)],[code(identity,'600001')],[])
    newer=publish(master,[member(identity)],[code(identity,'600002')],[],expected_head=first,evidence=b'updated mapping')
    with pytest.raises(MasterDataError,match='MASTER_VERSION_REWIND'):
        publish(master,[member(identity)],[code(identity,'600001')],[],expected_head=newer)
    assert master.resolve_code(first,'SSE','600001',date(2026,1,1))['instrument_id']==identity
    assert master.resolve_code(newer,'SSE','600002',date(2026,1,1))['instrument_id']==identity


def test_canonically_equivalent_identity_references_do_not_allocate_two_ids(master):
    first=master.register_identity('authority:é',asset_type='stock',exchange='SSE')
    assert master.register_identity('authority:e\u0301',asset_type='stock',exchange='SSE')==first


def test_explicit_bootstrap_rejects_an_unrelated_database_before_creating_tables(tmp_path):
    from sqlalchemy import text,inspect
    from app.db.governance_master_schema import initialize_master_schema
    engine=create_engine('sqlite:///'+(tmp_path/'unrelated.sqlite').as_posix())
    try:
        with engine.begin() as db:db.execute(text('CREATE TABLE business_data (id INTEGER PRIMARY KEY)'))
        with pytest.raises(ValueError,match='MASTER_BOOTSTRAP_SCOPE'):
            initialize_master_schema(engine)
        assert inspect(engine).get_table_names()==['business_data']
    finally:engine.dispose()


@pytest.mark.parametrize('damage',['json','evidence'])
def test_damaged_master_content_is_rejected_instead_of_returning_an_identity(master,damage):
    from sqlalchemy import text
    from app.services.governance_master_data import MasterDataError
    identity=master.register_identity('authority:A',asset_type='stock',exchange='SSE')
    version=publish(master,[member(identity)],[code(identity,'600001')],[])
    # Intentional storage-boundary fault injection, not an alternate writer.
    column,value=('document_json','{broken') if damage=='json' else ('evidence',b'corrupted evidence')
    with master.engine.begin() as db:
        db.execute(text('UPDATE master_data_versions SET '+column+'=:v WHERE version=:id'),{'v':value,'id':version})
    with pytest.raises(MasterDataError,match='VERSION_INTEGRITY'):
        master.resolve_code(version,'SSE','600001',date(2026,1,1))


@pytest.mark.parametrize('case,reason',[
    ('code_overlap','IDENTITY_INTERVAL_OVERLAP'),('alias_overlap','IDENTITY_INTERVAL_OVERLAP'),
    ('no_code','CODE_UNKNOWN'),('exchange','EXCHANGE_MISMATCH'),('unbound','ALIAS_CODE_UNBOUND'),
    ('namespace','LEGACY_TABLE_INVALID'),('evidence','EVIDENCE_REQUIRED'),('last_unknown','LIFECYCLE_UNKNOWN'),
    ('before_listing','OUTSIDE_LIFECYCLE'),('empty_interval','INTERVAL_INVALID'),('bool_id','ID_INVALID'),
    ('code_invalid','CODE_INVALID'),('identity_unknown','INSTRUMENT_UNKNOWN'),('date_invalid','DATE_INVALID')])
def test_unproven_or_ambiguous_mapping_is_rejected(master,case,reason):
    from app.services.governance_master_data import MasterDataError
    first=master.register_identity('authority:A',asset_type='stock',exchange='SSE')
    second=master.register_identity('authority:B',asset_type='stock',exchange='SSE')
    members=[member(first),member(second)]
    codes=[code(first,'600001'),code(second,'600002')]
    aliases=[alias(first,'symbols',77,'600001')]
    evidence=b'reviewed test evidence'
    if case=='code_overlap':codes.append(code(second,'600001'))
    elif case=='alias_overlap':aliases.append(alias(second,'symbols',77,'600002'))
    elif case=='no_code':codes=[];aliases=[]
    elif case=='exchange':codes[0]['exchange']='SZSE'
    elif case=='unbound':aliases[0]['code']='600002'
    elif case=='namespace':aliases[0]['legacy_table']='unregistered_table'
    elif case=='evidence':evidence=b''
    elif case=='last_unknown':members[0]['lifecycle_status']='delisted'
    elif case=='before_listing':codes[0]['valid_from']='2019-12-31'
    elif case=='empty_interval':codes[0]['valid_to']='2020-01-01'
    elif case=='bool_id':aliases[0]['legacy_id']=True
    elif case=='code_invalid':codes[0]['code']='UNKNOWN'
    elif case=='identity_unknown':members[0]['instrument_id']=999999
    elif case=='date_invalid':members[0]['list_date']='2020-1-1'
    with pytest.raises(MasterDataError,match=reason):publish(master,members,codes,aliases,evidence=evidence)


def test_concurrent_identity_registration_allocates_once_and_bootstrap_does_not_reset_ids(master):
    from concurrent.futures import ThreadPoolExecutor
    from app.db.governance_master_schema import initialize_master_schema
    from app.services.governance_master_data import MasterDataService
    def register(_):return MasterDataService(master.engine).register_identity('authority:A',asset_type='stock',exchange='SSE')
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(register,range(12)))
    assert len(set(results))==1
    initialize_master_schema(master.engine)
    assert master.register_identity('authority:B',asset_type='stock',exchange='SSE')>results[0]


def test_concurrent_mapping_publications_have_one_winner_and_keep_old_version(master):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.services.governance_master_data import MasterDataError,MasterDataService
    identity=master.register_identity('authority:A',asset_type='stock',exchange='SSE')
    old=publish(master,[member(identity)],[code(identity,'600001')],[])
    barrier=Barrier(2)
    def submit(value):
        service=MasterDataService(master.engine);barrier.wait(timeout=10)
        try:return ('ok',publish(service,[member(identity)],[code(identity,value)],[],expected_head=old,evidence=value.encode()))
        except MasterDataError as exc:return ('denied',str(exc))
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,['600002','600003']))
    assert [r[0] for r in results].count('ok')==1
    assert ('denied','MASTER_VERSION_CONFLICT') in results
    assert master.resolve_code(old,'SSE','600001',date(2026,1,1))['instrument_id']==identity


def test_snapshot_order_and_retry_are_idempotent_and_caller_edits_cannot_mutate_it(master):
    first=master.register_identity('authority:A',asset_type='stock',exchange='SSE')
    second=master.register_identity('authority:B',asset_type='stock',exchange='SSE')
    members=[member(first),member(second)];codes=[code(first,'600001'),code(second,'600002')]
    aliases=[alias(first,'symbols',77,'600001'),alias(second,'symbols',88,'600002')]
    version=publish(master,members,codes,aliases)
    observed=master.version_info(version)
    assert publish(master,list(reversed(members)),list(reversed(codes)),list(reversed(aliases)))==version
    assert master.version_info(version)==observed
    snapshot=master.instrument_snapshot(version);snapshot['members'][0]['list_date']='2099-01-01'
    assert master.resolve_code(version,'SSE','600001',date(2026,1,1))['instrument_id']==first


@pytest.mark.parametrize('lookup,reason',[
    ('latest','VERSION_REQUIRED'),('unknown_code','CODE_UNMAPPED'),('unknown_alias','LEGACY_UNMAPPED'),('pre_listing','OUTSIDE_LIFECYCLE')])
def test_read_requires_explicit_version_and_dated_mapping(master,lookup,reason):
    from app.services.governance_master_data import MasterDataError
    identity=master.register_identity('authority:A',asset_type='stock',exchange='SSE')
    version=publish(master,[member(identity)],[code(identity,'600001')],[])
    with pytest.raises(MasterDataError,match=reason):
        if lookup=='latest':master.resolve_code('latest','SSE','600001',date(2026,1,1))
        elif lookup=='unknown_code':master.resolve_code(version,'SSE','600002',date(2026,1,1))
        elif lookup=='unknown_alias':master.resolve_legacy(version,'symbols',identity,date(2026,1,1))
        else:master.validate_lifecycle(version,identity,date(2019,12,31))


@pytest.mark.parametrize('exchange',['SZSE','BSE','NYSE'])
def test_sse_page_does_not_certify_another_exchange_calendar(master,annual_page,exchange):
    from app.services.governance_master_data import MasterDataError
    from app.services.trade_calendar_sync import SOURCE_URL
    with pytest.raises(MasterDataError,match='CALENDAR_SOURCE_UNSUPPORTED'):
        master.publish_calendar(exchange=exchange,source_uri=SOURCE_URL,evidence=annual_page)


@pytest.mark.parametrize('body',[b'blocked',b'\xff',b'<h2>2026\xe5\xb9\xb4\xe4\xbc\x91\xe5\xb8\x82\xe5\xae\x89\xe6\x8e\x92</h2>'])
def test_invalid_calendar_refresh_cannot_damage_a_known_version(master,annual_page,body):
    from app.services.governance_master_data import MasterDataError
    from app.services.trade_calendar_sync import SOURCE_URL
    version=master.publish_calendar(exchange='SSE',source_uri=SOURCE_URL,evidence=annual_page)
    before=master.version_info(version)
    with pytest.raises(MasterDataError,match='CALENDAR_DOCUMENT_INVALID'):
        master.publish_calendar(exchange='SSE',source_uri=SOURCE_URL,evidence=body)
    assert master.version_info(version)==before
    assert master.trading_days(version,'SSE',date(2026,10,7),date(2026,10,9))==[date(2026,10,8)]


@pytest.mark.parametrize('mode,reason',[('disabled','DISABLED'),('production','PRODUCTION_NOT_ADMITTED')])
def test_official_capture_gate_prevents_any_supplier_invocation(master,tmp_path,monkeypatch,mode,reason):
    from app.services import trade_calendar_sync as service
    from app.services.data_source_control import PolicyDenied,RequestController,RequestPolicy
    c=RequestController(tmp_path/'control.sqlite')
    c.set_provider('sse',RequestPolicy(enabled=mode!='disabled',max_attempts=1))
    c.set_endpoint('CAL:official','sse',RequestPolicy(max_attempts=1),transport='requests',allowed_hosts=('www.sse.com.cn',))
    c.start_task('job',lane='incremental',max_requests=1,timeout_s=30)
    if mode=='production':monkeypatch.setenv('DATA_SOURCE_EXECUTION_MODE','production')
    monkeypatch.setattr(service.requests,'get',lambda *a,**k:pytest.fail('Blocked source invoked'))
    with c.scope('job'),pytest.raises(PolicyDenied,match=reason):service.capture_cn_calendar_version(master)
    assert c.statistics()['totals']['attempts']==0


def test_alias_history_survives_legacy_row_deletion_and_master_schema_is_not_auto_installed(master,db_session):
    from sqlalchemy import inspect
    from app.models.symbol import Symbol
    symbol=Symbol(id=77,symbol='600001',name='legacy',asset_type='stock',market='SH')
    db_session.add(symbol);db_session.commit()
    identity=master.register_identity('authority:A',asset_type='stock',exchange='SSE')
    version=publish(master,[member(identity)],[code(identity,'600001')],[alias(identity,'symbols',77,'600001')])
    db_session.delete(symbol);db_session.commit()
    assert master.resolve_legacy(version,'symbols',77,date(2026,1,1))['instrument_id']==identity
    assert 'instrument_master' not in inspect(db_session.get_bind()).get_table_names()


def test_reserved_ids_are_not_reused_and_published_identity_has_restrict_fk(master):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    reserved=master.register_identity('authority:abandoned',asset_type='stock',exchange='SSE')
    # Simulate deletion of an unpublished reservation at the database boundary.
    with master.engine.begin() as db:db.execute(text('DELETE FROM instrument_master WHERE instrument_id=:id'),{'id':reserved})
    identity=master.register_identity('authority:published',asset_type='stock',exchange='SSE')
    assert identity>reserved
    version=publish(master,[member(identity)],[code(identity,'600001')],[])
    with pytest.raises(IntegrityError),master.engine.begin() as db:
        db.execute(text('DELETE FROM instrument_master WHERE instrument_id=:id'),{'id':identity})
    assert master.resolve_code(version,'SSE','600001',date(2026,1,1))['instrument_id']==identity
