"""R4 public state service on real isolated databases; all proofs synthetic."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import pytest
from tests.test_governance_writer import system,candidate,annual_page,capture_request

pytestmark=pytest.mark.whitebox


@pytest.fixture
def governed(system):
    from app.services.governance_writer import FactWriter,WriterContract
    from app.services.governance_source_state import SourceStateService,migrate_source_state
    old,request,clock,base=system
    primary=WriterContract.daily_adapter(provider='eastmoney',endpoint='stock_zh_a_hist',asset_type='stock',instrument_family='sh_main',
        definition=base.document['definition'],evidence=b'SYNTHETIC eastmoney raw/unit/counter proof',publication_timezone='Asia/Shanghai',revision_order='integer')
    backup=WriterContract.daily_adapter(provider='sina',endpoint='stock_zh_a_daily',asset_type='stock',instrument_family='sh_main',
        definition=base.document['definition'],evidence=b'SYNTHETIC sina raw/unit proof',publication_timezone='Asia/Shanghai')
    writer=FactWriter(old.engine,contracts=[primary,backup],clock=clock)
    migrate_source_state(writer.engine)
    service=SourceStateService(writer)
    spec={'dataset':'daily_bars','quality_profile':'daily_bars/core','asset_type':'stock','instrument_family':'sh_main',
        'definition_id':primary.document['definition_id'],'comparison_policy':'bars-p13-v1','effective_from':'2026-01-01T00:00:00Z','effective_to':None,
        'members':[{'interface_id':'IF:stock_zh_a_hist','role':'primary','ordinal':0,'capability_requirements':['raw_evidence','family_unit_contract']},
                   {'interface_id':'IF:stock_zh_a_daily','role':'backup','ordinal':1,'capability_requirements':['raw_evidence','family_unit_contract']}]}
    policy=service.publish_policy(spec,evidence=b'SYNTHETIC policy review',expected_revision=0)
    request['contract_version']=primary.version
    capture_request(request)
    request['revision'].update(token='r8',order=8,order_evidence=b'SYNTHETIC counter 8')
    return service,request,clock,primary,backup,policy,spec


def backup_request(request,backup):
    other=deepcopy(request)
    other.update(contract_version=backup.version,wire_record={'open':'1.000000005','high':'1.1','low':'1','close':'1.05','volume':'1000','amount':None,'turnover':'0.001'})
    capture_request(other)
    other['revision'].update(token=None,order=None,order_evidence=None)
    return other


def test_registered_primary_initial_acceptance_binds_policy_and_keeps_dimensions_separate(governed):
    service,request,_,_,_,policy,_=governed
    result=service.submit(request,policy_version=policy,expected_revision=0)
    assert result['event']=='T01' and result['accepted']
    state=service.inspect('daily_bars',result['logical_key'])
    assert state['current']['data_version']==result['data_version']
    assert state['current']['source_state']=='canonical' and not state['degraded']
    assert state['transitions'][-1]['policy_version']==policy


def failed_primary(service,request,policy,revision=0):
    return service.record_failure('daily_bars',request['key'],policy_version=policy,expected_revision=revision,
        interface_id='IF:stock_zh_a_hist',reason='failed',evidence=b'SYNTHETIC source failure',batch_id=request['batch_id'])['transition_id']


def test_backup_requires_primary_failure_and_canonical_is_never_replaced_on_failure(governed):
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,backup,policy,_=governed
    other=backup_request(request,backup)
    with pytest.raises(WriterError,match='PRIMARY_FAILURE_REQUIRED'):
        service.submit(other,policy_version=policy,expected_revision=0)
    initial=service.submit(request,policy_version=policy,expected_revision=0)
    failure=failed_primary(service,request,policy,1)
    attempt=service.submit(other,policy_version=policy,expected_revision=1,failures=[failure])
    assert attempt['event']=='T05' and not attempt['accepted']
    assert service.inspect('daily_bars',initial['logical_key'])['current']['data_version']==initial['data_version']


def test_same_day_recovery_is_candidate_only_then_next_trading_day_recovers_atomically(governed):
    service,request,clock,_,backup,policy,_=governed
    clock.now=datetime(2026,6,1,8,tzinfo=timezone.utc)
    failure=failed_primary(service,request,policy)
    degraded=service.submit(backup_request(request,backup),policy_version=policy,expected_revision=0,failures=[failure])
    assert degraded['event']=='T04' and degraded['accepted']
    state=service.inspect('daily_bars',degraded['logical_key'])
    assert state['degraded'] and len(state['recoveries'])==1
    clock.advance()
    waiting=service.submit(request,policy_version=policy,expected_revision=1)
    assert waiting['event']=='T06' and not waiting['accepted']
    assert service.inspect('daily_bars',degraded['logical_key'])['current']['data_version']==degraded['data_version']
    clock.now=datetime(2026,6,2,8,tzinfo=timezone.utc)
    request['batch_id']='next-day-recovery'
    recovered=service.submit(request,policy_version=policy,expected_revision=1)
    assert recovered['event']=='T08' and recovered['accepted']
    state=service.inspect('daily_bars',degraded['logical_key'])
    assert not state['degraded'] and state['recoveries'][0]['status']=='done'
    assert [x['event'] for x in state['transitions']][-2:]==['T07','T08']
    assert service.writer.fact(degraded['data_version'])['available_at'] is not None


def degrade(service,request,backup,policy):
    failure=failed_primary(service,request,policy)
    return service.submit(backup_request(request,backup),policy_version=policy,expected_revision=0,failures=[failure])


def test_recovery_leases_expire_retry_with_capped_backoff_and_escalate_without_dropping(governed):
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,backup,policy,_=governed
    initial=degrade(service,request,backup,policy)
    first=service.lease_recoveries('worker-A',limit=10)
    assert len(first)==1 and first[0]['lease_until']-clock.now.replace(tzinfo=None)==timedelta(seconds=120)
    assert service.lease_recoveries('worker-B')==[]
    clock.now+=timedelta(seconds=121)
    second=service.lease_recoveries('worker-B')[0]
    assert second['lease_token']!=first[0]['lease_token']
    with pytest.raises(WriterError,match='LEASE_STALE'):service.fail_recovery(first[0],error='late worker',evidence=b'late')
    service.fail_recovery(second,error='still unavailable',evidence=b'SYNTHETIC upstream failed')
    state=service.inspect('daily_bars',initial['logical_key'])
    item=state['recoveries'][0]
    assert item['status']=='pending' and item['attempts']==1
    assert 120<=(item['next_attempt_at']-clock.now.replace(tzinfo=None)).total_seconds()<=132
    assert service.lease_recoveries('worker-C')==[]
    clock.now+=timedelta(days=1)
    assert service.lease_recoveries('worker-C')[0]['escalated_at'] is not None
    assert state['current']['data_version']==initial['data_version']


def test_every_degraded_date_is_recovered_not_only_yesterday(governed):
    service,request,clock,_,backup,policy,_=governed
    requests={};old=[]
    for day in ('2026-05-25','2026-05-26','2026-05-27','2026-05-28','2026-05-29'):
        req=deepcopy(request)
        req['key']['trade_date']=day
        end=(datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()
        req['time'].update(event_date=day,valid_from=day,valid_to=end)
        req['batch_id']='degrade-'+day
        result=degrade(service,req,backup,policy)
        requests[result['logical_key']]=req;old.append(result['data_version'])
    leases=service.lease_recoveries('batch-repair',limit=100)
    assert len(leases)==5
    clock.advance()
    for lease in leases:
        req=requests[lease['logical_key']];req['batch_id']='repair-'+lease['lease_token']
        result=service.submit(req,policy_version=policy,expected_revision=1,lease=lease)
        assert result['accepted']
        assert service.inspect('daily_bars',lease['logical_key'])['recoveries'][0]['status']=='done'
    assert service.lease_recoveries('again')==[]
    assert all(service.writer.fact(v)['available_at'] is not None for v in old)


def reviewers(service):
    from app.services.governance_reviews import ReviewAuthority
    from app.services.governance_source_state import SourceStateService
    return SourceStateService(service.writer,review_authority=ReviewAuthority({
        'fixture-human-A':{'kind':'human','key':b'isolated-key-A-at-least-thirty-two-bytes'},
        'fixture-human-B':{'kind':'human','key':b'isolated-key-B-at-least-thirty-two-bytes'},
        'fixture-assistant':{'kind':'assistant','key':b'isolated-key-agent-at-least-thirty-two'}}))


def signed_reviews(service,context,clock):
    import hmac,hashlib
    from app.services.governance_fact_contracts import canonical,digest
    result=[]
    for person,label in (('fixture-human-A','A'),('fixture-human-B','B')):
        evidence=('SYNTHETIC review '+label).encode()
        record={'reviewer_id':person,'decision':'approve','signed_at':clock.now.isoformat(),'valid_until':(clock.now+timedelta(hours=1)).isoformat(),'context_hash':context,'evidence_hash':digest(evidence)}
        signature=hmac.new(('isolated-key-'+label+'-at-least-thirty-two-bytes').encode(),canonical(record).encode(),hashlib.sha256).hexdigest()
        result.append({**record,'signature':signature,'evidence':evidence})
    return result


def test_recovery_difference_stays_conflict_until_two_distinct_verified_humans_approve(governed):
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,backup,policy,_=governed
    service=reviewers(service)
    old=degrade(service,request,backup,policy)
    clock.advance()
    request['wire_record'].update(最高='1.3',收盘='1.2')
    conflict=service.submit(request,policy_version=policy,expected_revision=1)
    assert conflict['event']=='T10' and conflict['outcome']=='conflict'
    assert service.inspect('daily_bars',old['logical_key'])['current']['data_version']==old['data_version']
    context=service.review_context(request,policy_version=policy,expected_revision=1,purpose='arbitration')
    reviews=signed_reviews(service,context,clock)
    with pytest.raises(WriterError,match='TWO_HUMAN_APPROVALS_REQUIRED'):
        service.arbitrate(request,policy_version=policy,expected_revision=1,reviews=[reviews[0],reviews[0]])
    approved=service.arbitrate(request,policy_version=policy,expected_revision=1,reviews=reviews)
    assert approved['event']=='T11' and approved['accepted']
    assert service.arbitrate(request,policy_version=policy,expected_revision=1,reviews=reviews)==approved
    assert service.inspect('daily_bars',old['logical_key'])['recoveries'][0]['status']=='done'
    assert service.writer.fact(old['data_version'])['payload']['raw_close']=='1.05000000'


def test_ordered_source_withdrawal_points_to_tombstone_and_never_falls_back(governed):
    service,request,clock,_,_,policy,_=governed
    first=service.submit(request,policy_version=policy,expected_revision=0)
    clock.advance()
    request['revision'].update(kind='withdrawal',token='r9',order=9,order_evidence=b'SYNTHETIC source withdrawal 9',supersedes_version=first['data_version'])
    request['wire_record']={'retraction_reason':'source withdrew this observation'}
    request['raw_response']=b'{"withdrawn":true,"revision":9}'
    tombstone=service.submit(request,policy_version=policy,expected_revision=1)
    assert tombstone['event']=='T12' and tombstone['accepted']
    result=service.resolve('daily_bars',first['logical_key'])
    assert result['missing']['reason']=='RETRACTED' and result['data_version']==tombstone['data_version']
    assert service.writer.fact(first['data_version'])['available_at'] is not None


def test_legacy_cannot_activate_without_mapping_and_bound_migration_approval(governed):
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,_,policy,_=governed
    service=reviewers(service)
    service.register_legacy('migration-fixture','old-table:77',raw=request['raw_response'],source_uri='fixture://legacy')
    with pytest.raises(WriterError,match='MAPPING_EVIDENCE_REQUIRED'):
        service.claim_legacy(request,migration_id='migration-fixture',legacy_key='old-table:77',policy_version=policy,expected_revision=0,mapping_evidence=b'')
    claim=service.claim_legacy(request,migration_id='migration-fixture',legacy_key='old-table:77',policy_version=policy,expected_revision=0,mapping_evidence=b'SYNTHETIC authority mapping proof')
    assert claim['event']=='T02' and not claim['accepted']
    assert service.inspect('daily_bars',claim['logical_key'])['current'] is None
    with pytest.raises(WriterError,match='MIGRATION_APPROVAL_REQUIRED'):
        service.submit(request,policy_version=policy,expected_revision=0)
    context=service.review_context(request,policy_version=policy,expected_revision=0,purpose='migration',migration_id='migration-fixture',legacy_key='old-table:77')
    approved=service.approve_migration(request,migration_id='migration-fixture',legacy_key='old-table:77',policy_version=policy,expected_revision=0,reviews=signed_reviews(service,context,clock))
    assert approved['event']=='T03' and approved['accepted']


def test_late_prior_version_is_ignored_even_when_its_parent_is_not_current(governed):
    service,request,clock,_,_,policy,_=governed
    original=deepcopy(request)
    first=service.submit(request,policy_version=policy,expected_revision=0)
    clock.advance()
    request['revision'].update(kind='correction',token='r9',order=9,order_evidence=b'counter9',supersedes_version=first['data_version'])
    request['wire_record']['收盘']='1.04';request['batch_id']='revision-9'
    newer=service.submit(request,policy_version=policy,expected_revision=1)
    assert newer['accepted']
    clock.advance();original['batch_id']='late-8'
    late=service.submit(original,policy_version=policy,expected_revision=2)
    assert late['event']=='T09' and late['outcome']=='ignored' and not late['accepted']
    state=service.inspect('daily_bars',newer['logical_key'])
    assert state['current']['data_version']==newer['data_version']
    assert service.submit(original,policy_version=policy,expected_revision=2)==late


@pytest.mark.parametrize('case',['duplicate_primary','no_primary','gap','duplicate_member','planned','excluded_minute','unresolved_definition','unknown_profile','bad_interval','capability'])
def test_invalid_policy_cannot_replace_the_approved_scope(governed,case):
    from app.services.governance_fact_contracts import WriterError
    service,request,_,_,_,policy,spec=governed
    bad=deepcopy(spec)
    if case=='duplicate_primary':bad['members'][1].update(role='primary',ordinal=0)
    elif case=='no_primary':bad['members'][0].update(role='validation')
    elif case=='gap':bad['members'][1]['ordinal']=2
    elif case=='duplicate_member':bad['members'].append(deepcopy(bad['members'][1]))
    elif case=='planned':bad['members'][1]['interface_id']='PLAN:Fuyao'
    elif case=='excluded_minute':bad['members'][1]['interface_id']='LEGACY:stock_zh_a_hist_min_em'
    elif case=='unresolved_definition':bad['definition_id']=None
    elif case=='unknown_profile':bad['quality_profile']='daily_bars/unregistered'
    elif case=='bad_interval':bad['effective_to']=bad['effective_from']
    else:bad['members'][0]['capability_requirements']=['unsupported-automatic-proof']
    with pytest.raises(WriterError):service.publish_policy(bad,evidence=b'SYNTHETIC invalid policy',expected_revision=1)
    assert service.submit(request,policy_version=policy,expected_revision=0)['accepted']


def test_daily_endpoint_cannot_be_misregistered_as_financial_product(governed):
    from app.services.governance_writer import FactWriter,WriterContract
    from app.services.governance_source_state import SourceStateService
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,_,_,spec=governed
    contract=WriterContract.profile(dataset='financial',profile='financial/net_profit',provider='eastmoney',endpoint='stock_zh_a_hist',asset_type='stock',instrument_family='sh_main',definition={'profit':'parent'},
        fields={'net_profit_parent':{'wire_field':'close','native_unit':'CNY','normalized_unit':'CNY','scale':'1','offset':'0'}},evidence=b'SYNTHETIC incompatible endpoint claim',publication_timezone='Asia/Shanghai')
    other=SourceStateService(FactWriter(service.engine,contracts=[contract],clock=clock))
    bad={**spec,'dataset':'financial','quality_profile':'financial/net_profit','definition_id':contract.document['definition_id'],'comparison_policy':'payload-exact-v1','members':[spec['members'][0]]}
    with pytest.raises(WriterError,match='POLICY_DATASET_MISMATCH'):other.publish_policy(bad,evidence=b'bad',expected_revision=0)


def test_non_bar_recovery_uses_exact_profile_fields_instead_of_stock_tolerance(governed,system):
    from app.services.governance_source_state import SourceStateService
    from tests.data_governance.test_index_profiles import setup,quote,row
    _,_,_,_,_,_,spec=governed
    index,args,contracts,clock,_=setup(system)
    service=SourceStateService(index.writer)
    spec={**spec,'dataset':'index','quality_profile':'index/core','asset_type':'index','instrument_family':'registered_index','definition_id':contracts['eastmoney'].document['definition_id'],'comparison_policy':'payload-exact-v1',
        'members':[{**spec['members'][0],'interface_id':'IF:index_zh_a_hist'},{**spec['members'][1],'interface_id':'IF:index_zh_a_daily'}]}
    policy=service.publish_policy(spec,evidence=b'SYNTHETIC index policy',expected_revision=0)
    request=quote(index,args,contracts,'sina')
    failure=service.record_failure('index',request['key'],policy_version=policy,expected_revision=0,interface_id='IF:index_zh_a_hist',reason='empty',evidence=b'empty',batch_id=request['batch_id'])
    old=service.submit(request,policy_version=policy,expected_revision=0,failures=[failure['transition_id']])
    clock.advance();changed=row();changed['收盘']='3060'
    request=quote(index,args,contracts,'eastmoney',[changed])
    conflict=service.submit(request,policy_version=policy,expected_revision=1)
    assert conflict['outcome']=='conflict' and not conflict['accepted']
    assert service.inspect('index',old['logical_key'])['current']['data_version']==old['data_version']


@pytest.mark.parametrize('case',['duplicate','assistant','unknown','signature','reject','future','expired','context','evidence'])
def test_invalid_human_approval_packets_cannot_change_current(governed,case):
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,backup,policy,_=governed
    service=reviewers(service);old=degrade(service,request,backup,policy);clock.advance()
    request['wire_record'].update(最高='1.3',收盘='1.2')
    service.submit(request,policy_version=policy,expected_revision=1)
    context=service.review_context(request,policy_version=policy,expected_revision=1,purpose='arbitration')
    approvals=signed_reviews(service,context,clock)
    if case=='duplicate':approvals[1]=deepcopy(approvals[0])
    elif case=='assistant':approvals[1]['reviewer_id']='fixture-assistant'
    elif case=='unknown':approvals[1]['reviewer_id']='Codex'
    elif case=='signature':approvals[1]['signature']='0'*64
    elif case=='reject':approvals[1]['decision']='reject'
    elif case=='future':
        future=deepcopy(clock);future.now+=timedelta(days=1);approvals=signed_reviews(service,context,future)
    elif case=='expired':clock.now+=timedelta(hours=2)
    elif case=='context':approvals=signed_reviews(service,'f'*64,clock)
    else:approvals[1]['evidence']=b'changed review bytes'
    with pytest.raises(WriterError,match='TWO_HUMAN_APPROVALS_REQUIRED'):service.arbitrate(request,policy_version=policy,expected_revision=1,reviews=approvals)
    assert service.inspect('daily_bars',old['logical_key'])['current']['data_version']==old['data_version']


def test_stale_arbitration_does_not_replay_against_a_recovered_pointer(governed):
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,backup,policy,_=governed
    service=reviewers(service);old=degrade(service,request,backup,policy);clock.advance()
    bad=deepcopy(request);bad['wire_record'].update(最高='1.3',收盘='1.2')
    service.submit(bad,policy_version=policy,expected_revision=1)
    context=service.review_context(bad,policy_version=policy,expected_revision=1,purpose='arbitration')
    approvals=signed_reviews(service,context,clock)
    request['revision'].update(token='r9',order=9,order_evidence=b'corrected source revision9')
    request['batch_id']='new-recovery';clock.advance()
    good=service.submit(request,policy_version=policy,expected_revision=1)
    assert good['accepted']
    with pytest.raises(WriterError,match='POINTER_STALE'):service.arbitrate(bad,policy_version=policy,expected_revision=1,reviews=approvals)
    assert service.inspect('daily_bars',old['logical_key'])['current']['data_version']==good['data_version']


def test_policy_switch_is_cas_and_never_itself_authorizes_cross_source_overwrite(governed):
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,backup,policy,spec=governed
    old=service.submit(request,policy_version=policy,expected_revision=0);clock.advance()
    switched=deepcopy(spec);switched['members'][0].update(role='backup',ordinal=1);switched['members'][1].update(role='primary',ordinal=0)
    with pytest.raises(WriterError,match='POLICY_STALE'):service.publish_policy(switched,evidence=b'SYNTHETIC switch',expected_revision=0)
    new=service.publish_policy(switched,evidence=b'SYNTHETIC switch',expected_revision=1)
    with pytest.raises(WriterError,match='POLICY_STALE'):service.submit(request,policy_version=policy,expected_revision=1)
    candidate=service.submit(backup_request(request,backup),policy_version=new,expected_revision=1)
    assert candidate['event']=='T10' and not candidate['accepted']
    assert service.inspect('daily_bars',old['logical_key'])['current']['data_version']==old['data_version']


def test_recovery_fetch_passes_real_controller_and_unregistered_source_never_contacts_supplier(governed,tmp_path):
    from app.services.data_source_control import RequestController,RequestPolicy,PolicyDenied
    service,request,clock,_,backup,policy,_=governed
    old=degrade(service,request,backup,policy)
    lease=service.lease_recoveries('controlled-recovery')[0]
    controller=RequestController(tmp_path/'source-control.sqlite')
    controller.start_task('repair',lane='backfill',max_requests=5,timeout_s=120)
    calls=[]
    with pytest.raises(PolicyDenied):service.fetch_recovery(lease,controller=controller,task_id='repair',fetch=lambda item:calls.append(item))
    assert not calls
    item=service.inspect('daily_bars',old['logical_key'])['recoveries'][0]
    assert item['status']=='pending' and item['attempts']==1
    controller.set_provider('eastmoney',RequestPolicy())
    controller.set_endpoint('stock_zh_a_hist','eastmoney',RequestPolicy())
    clock.now=item['next_attempt_at'].replace(tzinfo=timezone.utc);clock.advance()
    lease=service.lease_recoveries('controlled-recovery')[0]
    def fetch(item):
        calls.append(item);req=deepcopy(request);req['batch_id']='controlled-fetch';return req
    result=service.fetch_recovery(lease,controller=controller,task_id='repair',fetch=fetch)
    assert result['accepted'] and len(calls)==1 and controller.statistics()['totals']['attempts']==1


@pytest.mark.parametrize('phase',['source_transitions','source_candidates','recovery_done'])
def test_recovery_transaction_failure_never_commits_pointer_without_queue_completion(governed,phase):
    from sqlalchemy import event
    service,request,clock,_,backup,policy,_=governed
    old=degrade(service,request,backup,policy);before=service.inspect('daily_bars',old['logical_key']);clock.advance()
    hit=[]
    def fail(connection,cursor,statement,parameters,context,many):
        text=statement.lstrip().lower()
        match=(phase=='recovery_done' and text.startswith('update recovery_items ')) or (phase!='recovery_done' and text.startswith('insert into '+phase+' '))
        if match:hit.append(True);raise RuntimeError('SYNTHETIC R4 transaction failure')
    event.listen(service.engine,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError,match='SYNTHETIC R4'):service.submit(request,policy_version=policy,expected_revision=1)
    finally:event.remove(service.engine,'before_cursor_execute',fail)
    assert hit and service.inspect('daily_bars',old['logical_key'])==before
    assert service.submit(request,policy_version=policy,expected_revision=1)['accepted']


def test_different_workers_cannot_claim_the_same_live_recovery_lease(governed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    service,request,_,_,backup,policy,_=governed
    degrade(service,request,backup,policy);barrier=Barrier(2)
    def claim(worker):barrier.wait(timeout=15);return service.lease_recoveries(worker)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,['A','B']))
    assert sorted(map(len,results))==[0,1]


def test_concurrent_identical_state_submissions_return_one_token_and_one_transition(governed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    service,request,_,_,_,policy,_=governed
    barrier=Barrier(3)
    def send(_):
        barrier.wait(timeout=15)
        try:return service.submit(deepcopy(request),policy_version=policy,expected_revision=0)
        except Exception as error:return type(error).__name__+':'+str(error)
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(send,range(3)))
    assert all(isinstance(r,dict) for r in results),results
    assert len({r['write_token'] for r in results})==1
    state=service.inspect('daily_bars',results[0]['logical_key'])
    assert len(state['transitions'])==1 and state['current']['pointer_revision']==1


def test_repeated_legacy_claim_and_lease_failure_return_original_result(governed):
    service,request,_,_,backup,policy,_=governed
    service.register_legacy('m-idem','old:1',raw=request['raw_response'],source_uri='fixture://legacy')
    kwargs={'migration_id':'m-idem','legacy_key':'old:1','policy_version':policy,'expected_revision':0,'mapping_evidence':b'SYNTHETIC mapped identity/source'}
    first=service.claim_legacy(request,**kwargs)
    assert service.claim_legacy(request,**kwargs)==first
    degraded=degrade(service,request,backup,policy)
    lease=service.lease_recoveries('worker')[0]
    failed=service.fail_recovery(lease,error='source unavailable',evidence=b'failed')
    assert service.fail_recovery(lease,error='source unavailable',evidence=b'failed')==failed
    assert service.inspect('daily_bars',degraded['logical_key'])['recoveries'][0]['attempts']==1


def test_primary_fails_again_while_recovery_pending_keeps_one_item_and_backs_off(governed):
    service,request,clock,_,backup,policy,_=governed
    first=degrade(service,request,backup,policy);clock.advance()
    failure=failed_primary(service,request,policy,1)
    state=service.inspect('daily_bars',first['logical_key'])
    assert state['transitions'][-1]['event']=='T13' and state['transitions'][-1]['transition_id']==failure
    assert len(state['recoveries'])==1 and state['recoveries'][0]['attempts']==1
    assert state['recoveries'][0]['next_attempt_at']>clock.now.replace(tzinfo=None)
    assert state['current']['data_version']==first['data_version']


@pytest.mark.parametrize('field,value,accepted',[('close','1.06',True),('close','1.061',False),('volume','10.1',True),('volume','10.2',False),('amount','1070',True),('amount','1082',False)])
def test_bar_recovery_enforces_p13_boundaries_and_unknown_amount_is_not_zero(governed,field,value,accepted):
    service,request,clock,_,backup,policy,_=governed
    other=backup_request(request,backup)
    if field=='amount':other['wire_record']['amount']='1050';other['gate_results']['P02']='pass';request['gate_results']['P02']='pass'
    failure=failed_primary(service,request,policy)
    old=service.submit(other,policy_version=policy,expected_revision=0,failures=[failure]);clock.advance()
    request['wire_record'][{'close':'收盘','volume':'成交量','amount':'成交额'}[field]]=value
    result=service.submit(request,policy_version=policy,expected_revision=1)
    assert result['accepted'] is accepted
    assert service.inspect('daily_bars',old['logical_key'])['current']['data_version']==(result['data_version'] if accepted else old['data_version'])


def test_lower_priority_backup_requires_failures_of_all_earlier_members(governed):
    from app.services.governance_writer import FactWriter,WriterContract,WriterError
    from app.services.governance_source_state import SourceStateService
    service,request,clock,primary,backup,_,spec=governed
    third=WriterContract.daily_adapter(provider='tencent',endpoint='stock_zh_a_hist_tx',asset_type='stock',instrument_family='sh_main',definition=primary.document['definition'],evidence=b'SYNTHETIC tencent raw proof',publication_timezone='Asia/Shanghai')
    service=SourceStateService(FactWriter(service.engine,contracts=[primary,backup,third],clock=clock))
    spec=deepcopy(spec);spec['members'][1]['ordinal']=2
    spec['members'].append({'interface_id':'IF:stock_zh_a_hist_tx','role':'backup','ordinal':1,'capability_requirements':['raw_evidence','family_unit_contract']})
    policy=service.publish_policy(spec,evidence=b'SYNTHETIC explicit ordinal review',expected_revision=1)
    failed=failed_primary(service,request,policy)
    other=backup_request(request,backup)
    with pytest.raises(WriterError,match='PRIMARY_FAILURE_REQUIRED'):service.submit(other,policy_version=policy,expected_revision=0,failures=[failed])
    earlier=service.record_failure('daily_bars',request['key'],policy_version=policy,expected_revision=0,interface_id='IF:stock_zh_a_hist_tx',reason='empty',evidence=b'empty',batch_id=request['batch_id'])
    assert service.submit(other,policy_version=policy,expected_revision=0,failures=[failed,earlier['transition_id']])['accepted']


def test_unordered_withdrawal_requires_arbitration_and_unit_repair_never_uses_t09(governed):
    service,request,clock,_,_,policy,_=governed
    service=reviewers(service);first=service.submit(request,policy_version=policy,expected_revision=0);clock.advance()
    repair=deepcopy(request);repair['wire_record']['收盘']='1.04'
    repair['revision'].update(kind='repair',token='repair-9',order=9,order_evidence=b'SYNTHETIC local repair is not source chronology',supersedes_version=first['data_version'])
    candidate=service.submit(repair,policy_version=policy,expected_revision=1)
    assert candidate['event']=='T10' and not candidate['accepted']
    request['wire_record']={'retraction_reason':'uncertain source revision order'}
    request['revision'].update(kind='withdrawal',token=None,order=None,order_evidence=None,supersedes_version=first['data_version'])
    result=service.submit(request,policy_version=policy,expected_revision=1)
    assert not result['accepted'] and service.resolve('daily_bars',first['logical_key'])['missing'] is None
    context=service.review_context(request,policy_version=policy,expected_revision=1,purpose='arbitration')
    assert service.arbitrate(request,policy_version=policy,expected_revision=1,reviews=signed_reviews(service,context,clock))['accepted']
    assert service.resolve('daily_bars',first['logical_key'])['missing']['reason']=='RETRACTED'


def test_partial_core_can_be_admitted_for_an_independent_registered_profile(governed):
    from app.services.governance_writer import FactWriter,WriterContract
    from app.services.governance_source_state import SourceStateService
    service,request,clock,_,_,_,spec=governed
    contract=WriterContract.profile(dataset='financial',profile='financial/net_profit',provider='eastmoney',endpoint='stock_financial_analysis_indicator_em',asset_type='stock',instrument_family='sh_main',definition={'profit':'parent','period':'cumulative'},
        fields={'net_profit_parent':{'wire_field':'profit','native_unit':'CNY','normalized_unit':'CNY','scale':'1','offset':'0'}},evidence=b'SYNTHETIC financial field evidence',publication_timezone='Asia/Shanghai')
    service=SourceStateService(FactWriter(service.engine,contracts=[contract],clock=clock))
    spec={**spec,'dataset':'financial','quality_profile':'financial/net_profit','definition_id':contract.document['definition_id'],'comparison_policy':'payload-exact-v1',
        'members':[{**spec['members'][0],'interface_id':'IF:stock_financial_analysis_indicator_em'}]}
    policy=service.publish_policy(spec,evidence=b'SYNTHETIC independent profit policy',expected_revision=0)
    request.update(dataset='financial',profile='financial/net_profit',contract_version=contract.version,calendar_version=None,flags={},gate_results={},wire_record={'profit':'-100'},
        key={'instrument_id':request['key']['instrument_id'],'report_period':'2026-03-31','report_type':'q1','definition_id':contract.document['definition_id']})
    request['revision'].update(token=None,order=None,order_evidence=None)
    request['time'].update(event_date='2026-03-31',valid_from='2026-03-31',valid_to='2026-04-01')
    result=service.submit(request,policy_version=policy,expected_revision=0)
    assert result['accepted'] and result['request_quality_status']=='complete'
    assert service.resolve('financial',result['logical_key'])['quality_status']=='partial'


def test_r4_additive_migration_resumes_after_failure_and_preserves_master(candidate):
    from sqlalchemy import event
    from app.db.governance_migration import migrate_candidate,migration_status
    from app.services.governance_master_data import MasterDataService
    from app.services.governance_source_state import migrate_source_state
    migrate_candidate(candidate);master=MasterDataService(candidate)
    identity=master.register_identity('before-r4',asset_type='stock',exchange='SSE');before=master.identity(identity)
    def stop(connection,cursor,statement,parameters,context,many):
        if statement.lstrip().lower().startswith('create table recovery_items '):raise RuntimeError('SYNTHETIC R4 DDL failure')
    event.listen(candidate,'before_cursor_execute',stop)
    try:
        with pytest.raises(RuntimeError,match='R4 DDL'):migrate_source_state(candidate)
    finally:event.remove(candidate,'before_cursor_execute',stop)
    assert migration_status(candidate)['state']=='applied' and master.identity(identity)==before
    assert migrate_source_state(candidate)['revision']=='DSG-R4-001'
    assert migrate_source_state(candidate)['created_tables']==[] and master.identity(identity)==before


def test_local_repair_cannot_auto_recover_or_poison_source_revision_order(governed):
    service,request,clock,_,backup,policy,_=governed
    old=degrade(service,request,backup,policy);clock.advance()
    repair=deepcopy(request);repair['batch_id']='local-repair'
    repair['revision'].update(kind='repair',token='local-9',order=9,order_evidence=b'SYNTHETIC local repair counter',supersedes_version=old['data_version'])
    staged=service.submit(repair,policy_version=policy,expected_revision=1)
    assert staged['event']=='T10' and not staged['accepted']
    assert service.inspect('daily_bars',old['logical_key'])['current']['data_version']==old['data_version']
    request['batch_id']='real-primary-8'
    result=service.submit(request,policy_version=policy,expected_revision=1)
    assert result['event']=='T08' and result['accepted']


def test_expired_policy_blocks_recovery_before_supplier_request(governed,tmp_path):
    from app.services.data_source_control import RequestController,RequestPolicy
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,backup,_,spec=governed
    spec=deepcopy(spec);spec['effective_to']=(clock.now+timedelta(seconds=1)).isoformat()
    policy=service.publish_policy(spec,evidence=b'SYNTHETIC short policy validity',expected_revision=1)
    old=degrade(service,request,backup,policy);clock.advance()
    lease=service.lease_recoveries('expired-policy')[0]
    controller=RequestController(tmp_path/'source-control.sqlite')
    controller.set_provider('eastmoney',RequestPolicy());controller.set_endpoint('stock_zh_a_hist','eastmoney',RequestPolicy())
    controller.start_task('repair',lane='backfill',max_requests=5,timeout_s=120)
    calls=[]
    def fetch(item):calls.append(item);return deepcopy(request)
    with pytest.raises(WriterError,match='POLICY_EXPIRED'):
        service.fetch_recovery(lease,controller=controller,task_id='repair',fetch=fetch)
    assert not calls and service.inspect('daily_bars',old['logical_key'])['degraded']


def test_migration_with_existing_backup_becomes_conflict_then_requires_new_arbitration(governed):
    from app.services.governance_fact_contracts import WriterError
    service,request,clock,_,backup,policy,_=governed;service=reviewers(service)
    args={'migration_id':'migration-collision','legacy_key':'old-table:88'}
    service.register_legacy(**args,raw=request['raw_response'],source_uri='fixture://legacy')
    service.claim_legacy(request,**args,policy_version=policy,expected_revision=0,mapping_evidence=b'SYNTHETIC mapping')
    old=degrade(service,request,backup,policy);clock.advance()
    context=service.review_context(request,**args,policy_version=policy,expected_revision=1,purpose='migration')
    blocked=service.approve_migration(request,**args,policy_version=policy,expected_revision=1,reviews=signed_reviews(service,context,clock))
    assert blocked['event']=='T10' and not blocked['accepted']
    assert service.inspect('daily_bars',old['logical_key'])['current']['data_version']==old['data_version']
    with pytest.raises(WriterError,match='MIGRATION_APPROVAL_REQUIRED'):
        service.submit(request,policy_version=policy,expected_revision=1)
    context=service.review_context(request,policy_version=policy,expected_revision=1,purpose='arbitration')
    approved=service.arbitrate(request,policy_version=policy,expected_revision=1,reviews=signed_reviews(service,context,clock))
    assert approved['event']=='T11' and approved['accepted']
    assert service.register_legacy(**args,raw=request['raw_response'],source_uri='fixture://legacy')['state']=='canonical'


@pytest.mark.parametrize('target',['business','remote','relative'])
def test_r4_migration_rejects_out_of_scope_targets_before_connecting(tmp_path,target):
    from sqlalchemy import create_engine,event
    from app.services.governance_source_state import migrate_source_state
    from app.db.governance_migration import MigrationError
    url={'business':'sqlite:///'+(tmp_path/'production.sqlite').as_posix(),'remote':'mysql+pymysql://placeholder@192.0.2.1/codex_dsg_test','relative':'sqlite:///codex_dsg_relative.sqlite'}[target]
    engine=create_engine(url)
    def forbid(*args):raise AssertionError('target must be rejected before connection')
    event.listen(engine,'connect',forbid)
    try:
        with pytest.raises(MigrationError,match='CANDIDATE_SCOPE_REQUIRED'):migrate_source_state(engine)
    finally:engine.dispose()
