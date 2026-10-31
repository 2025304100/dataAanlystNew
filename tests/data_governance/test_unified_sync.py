"""M07.7 actual candidate jobs, R4 commits and live profile/dependency coverage."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import pytest
from app.services.governance_sync import GovernanceSyncService, install_sync_schema, SyncCancelled, prepare_requests
from app.services.governance_source_state import SourceStateService, migrate_source_state
from app.services.governance_source_interfaces import source_interface
from app.services.governance_fact_contracts import WriterError, canonical
from tests.test_governance_writer import system, candidate, annual_page, approval
from tests.test_governance_source_state import governed, failed_primary, backup_request
from tests.data_governance.test_event_ingestion import events

pytestmark = pytest.mark.whitebox


def manager(writer, source=None):
    install_sync_schema(writer.engine)
    return GovernanceSyncService(writer, source_service=source)


def specification(request, **overrides):
    return {'dataset': request['dataset'], 'profile': request['profile'], 'mode': 'force', 'adapter': 'prepared',
        'options': {}, 'expected_keys': [request['key']], 'expected_known': True,
        'contract_versions': [request['contract_version']], 'instrument_version': request['instrument_version'],
        'calendar_version': request['calendar_version'], 'policy_version': None, 'allow_degraded': False} | overrides


def policy_for(writer, contract):
    migrate_source_state(writer.engine); source = SourceStateService(writer); doc = contract.document
    spec = {**{k: doc['adapter'][k] for k in ['dataset', 'asset_type', 'instrument_family']},
        'quality_profile': doc.get('profile', 'daily_bars/core'), 'definition_id': doc['definition_id'], 'comparison_policy': 'bars-p13-v1' if doc['adapter']['dataset']=='daily_bars' else 'payload-exact-v1',
        'effective_from': '2026-01-01T00:00:00Z', 'effective_to': None,
        'members': [{'interface_id': source_interface(contract), 'role': 'primary', 'ordinal': 0, 'capability_requirements': []}]}
    return source, source.publish_policy(spec, evidence=b'SYNTHETIC candidate policy review, not ADMIT', expected_revision=0)


def test_stage_is_durable_but_never_coverage_and_terminal_retry_does_not_fetch_again(system):
    writer, request, clock, _ = system; service = manager(writer)
    job = service.create(specification(request), idempotency_key='stage')
    result = service.run(job, lambda work: [request])
    assert result['status'] == 'partial' and result['attempts'] == 1
    assert result['coverage']['covered_points'] == 0 and not result['coverage']['coverage_complete']
    reloaded = GovernanceSyncService(writer)
    assert reloaded.run(job, lambda work: pytest.fail('terminal job cannot refetch'))['result'] == result['result']
    writer.write(request, approval(writer, request)); clock.advance()
    assert reloaded.coverage(job)['coverage_complete']


def test_r4_acceptance_and_job_completion_share_one_transaction(governed):
    source, request, _, _, _, policy, _ = governed; service = manager(source.writer, source)
    job = service.create(specification(request, policy_version=policy), idempotency_key='r4')
    result = service.run(job, lambda work: [request])
    assert result['status'] == 'done' and result['coverage']['coverage_complete']
    assert result['result']['results'][0]['event'] == 'T01'
    assert source.resolve(request['dataset'], result['result']['results'][0]['logical_key'])['missing'] is None


def test_internal_gap_backfill_and_incremental_skip_use_current_versions(governed):
    from tests.test_governance_writer import capture_request
    source, request, clock, _, _, policy, _ = governed; service = manager(source.writer, source)
    rows = []
    for on in ['2026-05-27', '2026-05-28', '2026-05-29']:
        row = deepcopy(request); row['key']['trade_date'] = on
        row['time'].update(event_date=on, valid_from=on, valid_to=(datetime.fromisoformat(on) + timedelta(days=1)).date().isoformat())
        capture_request(row); rows.append(row)
    for row in [rows[0], rows[2]]: source.submit(row, policy_version=policy, expected_revision=0)
    job = service.create(specification(request, mode='backfill', policy_version=policy, expected_keys=[r['key'] for r in rows]), idempotency_key='gap')
    work_seen = []
    result = service.run(job, lambda work: work_seen.extend(work) or [rows[1]])
    assert work_seen == [rows[1]['key']] and result['status'] == 'done' and result['coverage']['coverage_complete']
    later = service.create(specification(request, mode='incremental', policy_version=policy, expected_keys=[r['key'] for r in rows]), idempotency_key='later')
    assert service.run(later, lambda work: pytest.fail('fully covered work must skip transport'))['result']['reason'] == 'already_covered'


def test_ordered_withdrawal_revokes_coverage_without_mutating_prior_result(governed):
    source, request, clock, _, _, policy, _ = governed; service = manager(source.writer, source)
    job = service.create(specification(request, policy_version=policy), idempotency_key='original')
    first = service.run(job, lambda work: [request]); saved = deepcopy(first['result'])
    clock.advance(); revision = deepcopy(request)
    revision['revision'].update(kind='withdrawal', token='r9', order=9, order_evidence=b'SYNTHETIC counter 9', supersedes_version=first['result']['results'][0]['data_version'])
    revision['raw_response'] = b'SYNTHETIC explicit source withdrawal'; revision['wire_record'] = {'retraction_reason': 'withdrawn'}
    withdrawal = service.create(specification(revision, policy_version=policy), idempotency_key='withdrawal')
    service.run(withdrawal, lambda work: [revision])
    assert service.coverage(job)['points'][0]['reason'] == 'withdrawn'
    assert not service.coverage(job)['coverage_complete'] and service.inspect(job)['result'] == saved


@pytest.mark.parametrize('failure', ['write', 'commit', 'cancel'])
def test_batch_failure_or_cancel_rolls_back_all_facts_receipts_and_r4_state(governed, monkeypatch, failure):
    from sqlalchemy import event
    source, request, _, _, _, policy, _ = governed; service = manager(source.writer, source)
    second = deepcopy(request); second['key']['trade_date'] = '2026-06-02'; second['time'].update(event_date='2026-06-02', valid_from='2026-06-02', valid_to='2026-06-03')
    requests = [request, second]; calls = []; stopped = [False]; injected = [False]
    job = service.create(specification(request, policy_version=policy, expected_keys=[r['key'] for r in requests]), idempotency_key='rollback-' + failure)
    real = source.writer.write
    def write(*args, **kwargs):
        calls.append(True)
        result = real(*args, **kwargs)
        if len(calls) == 1 and failure == 'cancel': stopped[0] = True
        if len(calls) == 2 and failure == 'write': raise RuntimeError('injected fact write failure')
        return result
    monkeypatch.setattr(source.writer, 'write', write)
    def commit(db):
        if db.info.pop('fail_sync_commit', False): raise RuntimeError('injected actual commit failure')
    def before_execute(conn, cursor, statement, parameters, context, many):
        if failure == 'commit' and not injected[0] and statement.lstrip().upper().startswith('UPDATE GOVERNANCE_SYNC_JOBS') and len(calls) == 2:
            injected[0] = True
            conn.info['fail_sync_commit'] = True
    event.listen(service.engine, 'commit', commit); event.listen(service.engine, 'before_cursor_execute', before_execute)
    try:
        with pytest.raises((RuntimeError, SyncCancelled)):
            service.run(job, lambda work: requests, should_cancel=lambda: stopped[0])
    finally:
        event.remove(service.engine, 'commit', commit); event.remove(service.engine, 'before_cursor_execute', before_execute)
    assert service.inspect(job)['status'] == ('cancelled' if failure == 'cancel' else 'failed')
    for row in requests:
        key = source.writer.preview(row)['logical_key']
        assert source.writer.audit(key)['counts']['facts'] == 0
        assert source.inspect(row['dataset'], key)['current'] is None
        assert source.inspect(row['dataset'], key)['transitions'] == []


def test_acquisition_failure_is_not_empty_and_explicit_retry_keeps_job_identity(system):
    writer, request, _, _ = system; service = manager(writer); job = service.create(specification(request), idempotency_key='retry')
    def offline(work): raise ConnectionError('synthetic upstream failure')
    with pytest.raises(ConnectionError): service.run(job, offline)
    assert service.inspect(job)['status'] == 'failed'
    assert service.coverage(job)['covered_points'] == 0
    service.retry(job)
    retried = service.run(job, lambda work: [request])
    assert retried['attempts'] == 2
    assert [h['status'] for h in retried['attempt_history']] == ['failed', 'partial']
    assert retried['attempt_history'][0]['result']['reason'] == 'synthetic upstream failure'
    assert service.create(specification(request), idempotency_key='retry') == job
    with pytest.raises(WriterError, match='SYNC_IDEMPOTENCY_CONFLICT'):
        service.create(specification(request, mode='backfill'), idempotency_key='retry')


@pytest.mark.parametrize('known', [False, True])
def test_empty_unknown_and_zero_denominators_never_become_full_coverage(system, known):
    writer, request, _, _ = system; service = manager(writer)
    job = service.create(specification(request, expected_keys=[], expected_known=known), idempotency_key='empty-' + str(known))
    result = service.run(job, lambda work: [])
    assert result['status'] == 'empty' and result['coverage']['coverage_ratio'] is None
    assert not result['coverage']['coverage_complete']
    assert result['coverage']['denominator_status'] == ('zero' if known else 'unknown')


def test_cancel_before_fetch_and_during_fetch_are_persistent(system):
    writer, request, _, _ = system; service = manager(writer)
    first = service.create(specification(request), idempotency_key='cancel-queued'); service.cancel(first)
    assert service.run(first, lambda work: pytest.fail('cancelled queued job fetched'))['status'] == 'cancelled'
    second = service.create(specification(request), idempotency_key='cancel-running')
    def acquire(work): service.cancel(second); return [request]
    with pytest.raises(SyncCancelled): service.run(second, acquire)
    assert service.inspect(second)['status'] == 'cancelled' and service.coverage(second)['covered_points'] == 0


def test_cancelled_running_job_reclaimed_without_acquisition_closes_attempt(system):
    writer, request, _, _ = system; service = manager(writer)
    job = service.create(specification(request), idempotency_key='cancel-pending-reclaim')
    token = service.claim(job); service.cancel(job)
    result = manager(writer).run(job, lambda work: pytest.fail('cancelled work must not acquire'))
    assert result['status'] == 'cancelled' and result['attempts'] == 1
    assert len(result['attempt_history']) == 1 and result['attempt_history'][0]['status'] == 'cancelled'
    assert result['attempt_history'][0]['finished_at'] is not None
    assert result['lease_token'] is None and result['lease_until'] is None
    assert not result['coverage']['coverage_complete']
    with pytest.raises(WriterError, match='SYNC_LEASE_STALE'): service.renew(job, token)


def test_restart_expired_lease_fences_old_worker_and_renew_is_bounded(system):
    writer, request, clock, _ = system; service = manager(writer)
    job = service.create(specification(request), idempotency_key='crash')
    old = service.claim(job)
    with pytest.raises(WriterError, match='SYNC_LEASE_HELD'): manager(writer).claim(job)
    clock.advance(); service.renew(job, old)
    assert service.inspect(job)['lease_until'] == clock.now.replace(tzinfo=None) + timedelta(seconds=120)
    clock.now += timedelta(seconds=121); fresh = manager(writer).claim(job)
    with pytest.raises(WriterError, match='SYNC_LEASE_STALE'): service.renew(job, old)
    assert fresh != old
    clock.now += timedelta(seconds=121)
    assert manager(writer).run(job, lambda work: [request])['attempts'] == 3


@pytest.mark.parametrize('change', ['key', 'profile', 'contract', 'calendar', 'duplicate'])
def test_scope_mismatch_or_duplicate_cannot_commit_any_fact(system, change):
    writer, request, _, _ = system; service = manager(writer); job = service.create(specification(request), idempotency_key='bad-' + change)
    wrong = deepcopy(request)
    if change == 'key': wrong['key']['trade_date'] = '2026-06-02'; wrong['time'].update(event_date='2026-06-02', valid_from='2026-06-02', valid_to='2026-06-03')
    elif change == 'profile': wrong['profile'] = 'daily_bars/close'
    elif change == 'contract': wrong['contract_version'] = '0' * 64
    elif change == 'calendar': wrong['calendar_version'] = '0' * 64
    with pytest.raises((WriterError, ValueError)):
        service.run(job, lambda work: [wrong, wrong] if change == 'duplicate' else [wrong])
    assert service.inspect(job)['status'] == 'failed' and service.coverage(job)['covered_points'] == 0


def test_financial_profile_specific_coverage_does_not_promote_core(system):
    from tests.data_governance.test_fundamental_sync import environment, archive, financial
    product, options, _, contract = environment(system, profile='financial/net_profit')
    request = product.requests(archive(options['product'], [financial()]), **options)[0]
    source, policy = policy_for(product.writer, contract); service = manager(product.writer, source)
    job = service.create(specification(request, policy_version=policy), idempotency_key='profit')
    result = service.run(job, lambda work: [request]); assert result['coverage']['coverage_complete']
    core = service.create(specification(request, profile='financial/core'), idempotency_key='core')
    assert service.coverage(core)['points'][0]['reason'] == 'partial'


def test_etf_dependency_revision_invalidates_size_coverage_and_local_policy_is_explicit(system):
    from tests.data_governance.test_etf_profiles import setup, source, accept, derive
    product, args, clock, contracts, _ = setup(system)
    nav = source(product, args, contracts, 'nav'); nv = accept(product, nav)
    shares = source(product, args, contracts, 'shares'); sv = accept(product, shares)
    size = derive(product, args, contracts, 'size', nav_data_version=nv, shares_data_version=sv)
    executor, policy = policy_for(product.writer, contracts['size']); service = manager(product.writer, executor)
    assert source_interface(contracts['size']) == 'LOCAL:etf_size'
    job = service.create(specification(size, policy_version=policy), idempotency_key='size')
    assert service.run(job, lambda work: [size])['coverage']['coverage_complete']
    old_result = service.inspect(job)['result']; clock.advance()
    nav['wire_record']['nav_official'] = '3'  # rebuild the bound source archive, never spoof just the payload
    new_nav = source(product, args, contracts, 'nav', value='3')
    new_nav['revision'].update(kind='correction', token='reviewed-2', supersedes_version=nv)
    product.writer.write(new_nav, approval(product.writer, new_nav, revision=1, event='T11'))
    assert service.coverage(job)['points'][0]['reason'] == 'dependency_blocked'
    assert service.inspect(job)['result'] == old_result


@pytest.mark.parametrize('dataset', ['corporate_action', 'share_capital'])
def test_official_event_jobs_keep_announcements_and_invalidate_after_notice_withdrawal(events, dataset):
    writer, contracts, request, notice, raw, clock = events; request = deepcopy(request)
    if dataset == 'share_capital':
        request.update(dataset=dataset, profile=dataset + '/core', contract_version=contracts[dataset].version,
            key={'instrument_id': request['key']['instrument_id'], 'effective_date': '2026-06-02', 'share_class': 'A'},
            wire_record={'share_class': 'A', 'share_definition': 'cn-issued-tradable-freefloat-v1', 'total_shares': '1000000',
                'float_shares': '800000', 'free_float_shares': '700000', 'announcement_id': 'A001',
                'announcement_data_version': notice, 'event_type': 'stock_dividend', 'change_reason': 'bonus'})
        request['time']['valid_to'] = None
    executor, policy = policy_for(writer, contracts[dataset]); service = manager(writer, executor)
    job = service.create(specification(request, policy_version=policy), idempotency_key='event-' + dataset)
    assert service.run(job, lambda work: [request])['coverage']['coverage_complete']
    announcement = deepcopy(request); announcement.update(dataset='announcement', profile='announcement/core',
        key={'authority': 'SSE', 'announcement_id': 'A001'}, contract_version=contracts['announcement'].version,
        instrument_version=None, wire_record={'retraction_reason': 'notice withdrawn'})
    announcement['time']['valid_to'] = '2026-06-03'
    announcement['revision'].update(kind='withdrawal', token='withdrawal', order=None, order_evidence=None, supersedes_version=notice)
    clock.advance(); writer.write(announcement, approval(writer, announcement, revision=1, event='T11'))
    assert service.coverage(job)['points'][0]['reason'] == 'dependency_blocked'


@pytest.mark.parametrize('product', ['nbs_cpi', 'fred:UNRATE', 'pboc_new_credit'])
def test_macro_policy_binding_and_actual_adapter_dispatch(system, product):
    from app.services.governance_macro import MacroSyncService, macro_contract, reviewed_release_capture
    from tests.data_governance.test_macro_releases import fred_capture, official_row
    from app.services.governance_writer import FactWriter
    contract = macro_contract(product, evidence=b'SYNTHETIC macro contract'); writer = FactWriter(system[0].engine, contracts=[contract], clock=system[2])
    if product.startswith('fred:'):
        capture = fred_capture(); uri = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE'
    else:
        uri = 'https://www.' + ('stats.gov.cn' if product == 'nbs_cpi' else 'pbc.gov.cn') + '/synthetic'
        capture = reviewed_release_capture(product, b'SYNTHETIC actual release body', records=[official_row(product)], review_evidence=b'SYNTHETIC review', source_uri=uri)
    options = dict(product=product, contract_version=contract.version, start='2026-05-01', end_exclusive='2026-06-01', source_uri=uri)
    request = MacroSyncService(writer).requests(capture, **options, batch_id='template')[0]
    source, policy = policy_for(writer, contract); service = manager(writer, source)
    job = service.create(specification(request, adapter='macro', options=options, policy_version=policy), idempotency_key=product)
    result = service.run(job, lambda work: capture)
    assert result['status'] == 'done' and result['coverage']['coverage_complete']
    assert result['result']['results'][0]['event'] == 'T01'


def test_daily_dispatch_reuses_bound_capture_and_preserves_empty_raw_evidence(system):
    from tests.data_governance.test_daily_sync import capture, args, row
    writer, request, _, contract = system; service = manager(writer)
    options = args(request); options.pop('batch_id')
    raw = capture(contract, [row(request)])
    job = service.create(specification(request, adapter='daily_bars', options=options), idempotency_key='daily-adapter')
    assert service.run(job, lambda work: raw)['status'] == 'partial'
    empty = capture(contract, []); other = service.create(specification(request, adapter='daily_bars', options=options), idempotency_key='daily-empty')
    result = service.run(other, lambda work: empty)
    assert result['status'] == 'empty' and not result['coverage']['coverage_complete']
    from app.services.governance_fact_contracts import digest
    assert writer.evidence(digest(empty))['raw_bytes'] == empty


def test_fred_disabled_request_through_job_is_failed_not_empty(system, tmp_path):
    from tests.test_data_source_execution import controller
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    from app.services.governance_macro import capture_fred
    writer, request, _, _ = system; service = manager(writer)
    job = service.create(specification(request), idempotency_key='denied')
    c = controller(tmp_path, 'fred:UNRATE'); c.set_provider('provider', RequestPolicy(enabled=False), expected_version=1)
    with c.scope('job'), pytest.raises(PolicyDenied, match='DISABLED'):
        service.run(job, lambda work: capture_fred('fred:UNRATE'))
    assert service.inspect(job)['status'] == 'failed' and c.statistics()['totals']['attempts'] == 0


def test_candidate_http_api_runs_real_writer_and_reports_live_coverage(governed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.governance_sync import create_sync_router
    source, request, _, _, _, policy, _ = governed; service = manager(source.writer, source)
    app = FastAPI(); app.include_router(create_sync_router(service, {'prepared': lambda spec, work: [request]}))
    client = TestClient(app)
    created = client.post('/governance-sync/jobs', json={'idempotency_key': 'api', 'spec': specification(request, policy_version=policy)})
    assert created.status_code == 200; job = created.json()['job_id']
    response = client.post('/governance-sync/jobs/' + job + '/run')
    assert response.status_code == 200 and response.json()['status'] == 'done'
    state = client.get('/governance-sync/jobs/' + job).json()
    assert state['coverage']['coverage_complete'] and 'lease_token' not in state
    assert state['result']['coverage_certificate'] is False
    conflict = client.post('/governance-sync/jobs', json={'idempotency_key': 'api', 'spec': specification(request, mode='backfill', policy_version=policy)})
    assert conflict.status_code == 409
    assert client.get('/governance-sync/jobs/' + '0' * 64).status_code == 404


@pytest.mark.parametrize('product', ['eastmoney_valuation','baostock_valuation','eastmoney_financial','flow_history','lhb_institution','hot_rank',
    'nav','shares','iopv','index_eastmoney','index_sina','index_tencent','announcement_SSE','announcement_SZSE','announcement_CNINFO'])
def test_existing_product_adapters_reach_the_same_durable_task_and_writer(system, product):
    if product in {'eastmoney_valuation','baostock_valuation','eastmoney_financial'}:
        from tests.data_governance.test_fundamental_sync import environment, archive, financial, valuation
        parser, options, clock, contract = environment(system, product, profile='financial/net_profit' if product=='eastmoney_financial' else None)
        raw = archive(product, [financial() if product=='eastmoney_financial' else valuation(product)])
        adapter = 'fundamentals'; writer = parser.writer; request = parser.requests(raw, **options)[0]
    elif product in {'flow_history','lhb_institution','hot_rank'}:
        from tests.data_governance.test_activity_sync import setup, archive, flow, lhb, rank, seat_proof
        parser, options, clock = setup(system, product)
        record = flow() if product=='flow_history' else rank() if product=='hot_rank' else lhb(seat_evidence=seat_proof(options['instrument_id']))
        raw = archive(product, [record]); adapter = 'activity'; writer = parser.writer
        request = parser.requests(raw, **options)[0]; contract = writer.contracts[request['contract_version']]
    elif product in {'nav','shares','iopv'}:
        from tests.data_governance.test_etf_profiles import setup, source
        parser, args, clock, contracts, _ = setup(system); request = source(parser, args, contracts, product)
        raw = request['raw_response']; contract = contracts[product]; writer = parser.writer; adapter = 'etf'
        options = dict(**args, product=product, contract_version=contract.version, source_uri=request['source_uri'], start='2026-06-01', end_exclusive='2026-06-03')
    elif product.startswith('index_'):
        from tests.data_governance.test_index_profiles import setup, quote
        provider = product.split('_',1)[1]; parser, args, contracts, clock, _ = setup(system)
        request = quote(parser, args, contracts, provider); raw = request['raw_response']; contract = contracts[provider]; writer = parser.writer; adapter = 'index'
        options = dict(**args, contract_version=contract.version, start='2026-06-01', end_exclusive='2026-06-03', source_uri=request['source_uri'])
    else:
        from app.services.governance_releases import announcement_contract, AnnouncementSyncService, PUBLIC_RELEASES
        from app.services.governance_writer import FactWriter
        authority = product.split('_',1)[1]; contract = announcement_contract(authority, evidence=b'SYNTHETIC authority body contract')
        writer = FactWriter(system[0].engine, contracts=[contract], clock=system[2]); adapter='announcement'; raw=b'SYNTHETIC Sunday official announcement'
        options=dict(authority=authority, announcement_id='Sunday-A', contract_version=contract.version,
            source_uri='https://www.'+PUBLIC_RELEASES[authority][0]+'/synthetic', publication_precision='exact', published_at='2026-05-31T10:00:00+08:00')
        request = AnnouncementSyncService(writer).request(raw, **options, batch_id='template')
    options.pop('batch_id',None)
    executor, policy = policy_for(writer, contract); service=manager(writer, executor)
    job=service.create(specification(request, adapter=adapter, options=options, policy_version=policy), idempotency_key=product)
    result=service.run(job, lambda work: raw)
    assert result['status']=='done' and result['coverage']['coverage_complete']
    version=result['result']['results'][0]['data_version']
    assert writer.fact(version)['dataset']==request['dataset'] and writer.evidence(writer.fact(version)['raw_hash'])['raw_bytes']==raw


def test_stale_inflight_worker_cannot_overwrite_successor_result(system):
    writer, request, clock, _=system; service=manager(writer)
    job=service.create(specification(request), idempotency_key='inflight')
    def acquire(work):
        clock.now += timedelta(seconds=121)
        successor=GovernanceSyncService(writer).run(job, lambda todo: [request])
        assert successor['status']=='partial'
        return [request]
    with pytest.raises(WriterError, match='SYNC_LEASE_STALE'): service.run(job, acquire)
    assert service.inspect(job)['status']=='partial' and service.inspect(job)['attempts']==2
    assert writer.audit(writer.preview(request)['logical_key'])['counts']['facts']==1


def test_cross_dataset_api_jobs_use_independent_versions_and_never_spread_withdrawal(system):
    from app.services.governance_writer import FactWriter
    from app.services.governance_macro import macro_contract, MacroSyncService
    from tests.data_governance.test_macro_releases import fred_capture
    from app.services.governance_releases import announcement_contract, AnnouncementSyncService
    from app.api.routes.governance_sync import create_sync_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    base, daily, clock, dc=system; mc=macro_contract('fred:UNRATE',evidence=b'SYNTHETIC macro review'); ac=announcement_contract('SSE',evidence=b'SYNTHETIC public body review')
    writer=FactWriter(base.engine,contracts=[dc,mc,ac],clock=clock)
    macro=MacroSyncService(writer).requests(fred_capture(),product='fred:UNRATE',contract_version=mc.version,start='2026-05-01',end_exclusive='2026-06-01',source_uri='https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE',batch_id='macro')[0]
    notice=AnnouncementSyncService(writer).request(b'SYNTHETIC Sunday disclosure',authority='SSE',announcement_id='cross-A',contract_version=ac.version,
        source_uri='https://www.sse.com.cn/synthetic',batch_id='announcement',publication_precision='exact',published_at='2026-05-31T10:00:00+08:00')
    requests={r['dataset']:r for r in [daily,macro,notice]}; source,dp=policy_for(writer,dc); _,mp=policy_for(writer,mc); _,ap=policy_for(writer,ac)
    service=manager(writer,source); app=FastAPI(); app.include_router(create_sync_router(service,{'prepared':lambda spec,work:[requests[spec['dataset']]]})); client=TestClient(app)
    jobs=[]; originals=[]
    for request,policy in [(daily,dp),(macro,mp),(notice,ap)]:
        result=client.post('/governance-sync/jobs',json={'spec':specification(request,policy_version=policy),'idempotency_key':'cross-'+request['dataset']})
        job=result.json()['job_id']; result=client.post('/governance-sync/jobs/'+job+'/run')
        assert result.status_code==200 and result.json()['coverage']['coverage_complete']; jobs.append(job); originals.append(result.json()['result'])
    assert len({r['results'][0]['data_version'] for r in originals})==3
    withdrawn=deepcopy(daily); withdrawn['revision'].update(kind='withdrawal',supersedes_version=originals[0]['results'][0]['data_version'])
    withdrawn['wire_record']={'retraction_reason':'reviewed actual source withdrawal'}; clock.advance()
    writer.write(withdrawn,approval(writer,withdrawn,revision=1,event='T11'))
    assert not service.coverage(jobs[0])['coverage_complete']
    assert all(service.coverage(job)['coverage_complete'] for job in jobs[1:])
    assert [service.inspect(job)['result'] for job in jobs]==originals


def test_backup_coverage_is_explicitly_degraded_and_does_not_fill_primary_goal(governed):
    source,request,_,_,backup,policy,_=governed; service=manager(source.writer,source)
    other=backup_request(request,backup)
    key=source.writer.preview(other)['logical_key']
    job=service.create(specification(other,policy_version=policy),idempotency_key='backup')
    def acquire(work):
        service.record_failure(job,request['key'],interface_id='IF:stock_zh_a_hist',reason='failed',evidence=b'SYNTHETIC controlled supplier failure')
        return [other]
    result=service.run(job,acquire)
    assert result['result']['results'][0]['accepted'] and not result['coverage']['coverage_complete']
    assert result['coverage']['points'][0]['degraded'] and result['coverage']['points'][0]['reason']=='degraded'
    usable=service.create(specification(other,allow_degraded=True),idempotency_key='degraded-allowed')
    assert service.coverage(usable)['coverage_complete']


def test_sync_worker_and_readers_keep_database_privilege_boundaries(governed):
    from app.services.governance_write_boundary import install_boundary
    from app.services.governance_database_roles import candidate_roles
    from app.services.governance_writer import FactWriter
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    source,request,clock,_,_,_,_=governed
    install_boundary(source.engine,instrument_version=request['instrument_version']); install_sync_schema(source.engine)
    with candidate_roles(source.engine) as roles:
        worker=FactWriter(roles['writer'],contracts=list(source.writer.contracts.values()),clock=clock)
        service=GovernanceSyncService(worker);job=service.create(specification(request),idempotency_key='role')
        assert service.run(job,lambda work:[request])['status']=='partial'
        reader=GovernanceSyncService(FactWriter(roles['reader'],contracts=list(source.writer.contracts.values()),clock=clock))
        assert reader.inspect(job)['status']=='partial' and not reader.coverage(job)['coverage_complete']
        for role in ['reader','legacy']:
            with pytest.raises(DBAPIError):
                with roles[role].begin() as db:db.execute(text('UPDATE governance_sync_jobs SET cancel_requested=1'))
        for sql in ['UPDATE fact_versions SET payload_json=\'{}\'','DELETE FROM governance_sync_attempts']:
            with pytest.raises(DBAPIError):
                with roles['writer'].begin() as db:db.execute(text(sql))


def test_non_market_recovery_uses_explicit_control_calendar_without_changing_period(system):
    from app.services.governance_macro import macro_contract, MacroSyncService, reviewed_release_capture
    from tests.data_governance.test_macro_releases import official_row
    from app.services.governance_writer import FactWriter
    contracts={p:macro_contract(p,evidence=b'SYNTHETIC LPR source definition') for p in ['pboc_lpr_1y','chinamoney_lpr_1y']}
    writer=FactWriter(system[0].engine,contracts=list(contracts.values()),clock=system[2]); parser=MacroSyncService(writer)
    source,policy=policy_for(writer,contracts['pboc_lpr_1y'])
    spec={'dataset':'macro','quality_profile':'macro/core','asset_type':'macro','instrument_family':'economic_indicator',
        'definition_id':contracts['pboc_lpr_1y'].document['definition_id'],'comparison_policy':'payload-exact-v1',
        'effective_from':'2026-01-01T00:00:00Z','effective_to':None,
        'members':[{'interface_id':'PLAN:PBOC_direct','role':'primary','ordinal':0,'capability_requirements':[]},
                   {'interface_id':'PLAN:Chinamoney_direct','role':'backup','ordinal':1,'capability_requirements':[]}]}
    policy=source.publish_policy(spec,evidence=b'SYNTHETIC explicit recovery order',expected_revision=1)
    requests={}
    for p,host in [('pboc_lpr_1y','pbc.gov.cn'),('chinamoney_lpr_1y','chinamoney.com.cn')]:
        uri='https://www.'+host+'/synthetic';raw=reviewed_release_capture(p,b'SYNTHETIC release',records=[official_row(p)],review_evidence=b'SYNTHETIC review',source_uri=uri)
        requests[p]=parser.requests(raw,product=p,contract_version=contracts[p].version,start='2026-05-01',end_exclusive='2026-06-01',source_uri=uri,batch_id='A')[0]
    service=manager(writer,source); other=requests['chinamoney_lpr_1y']
    job=service.create(specification(other,policy_version=policy),idempotency_key='macro-backup')
    def acquire(work):
        service.record_failure(job,other['key'],interface_id='PLAN:PBOC_direct',reason='failed',evidence=b'SYNTHETIC primary failure')
        return [other]
    service.run(job,acquire)
    system[2].advance()
    primary=requests['pboc_lpr_1y']; missing=service.create(specification(primary,policy_version=policy),idempotency_key='missing-control')
    with pytest.raises(WriterError,match='RECOVERY_CALENDAR_REQUIRED'): service.run(missing,lambda work:[primary])
    recovered=service.create(specification(primary,policy_version=policy,recovery_calendar_version=system[1]['calendar_version']),idempotency_key='macro-recovery')
    result=service.run(recovered,lambda work:[primary])
    assert result['coverage']['coverage_complete'] and result['result']['results'][0]['event']=='T08'
    assert writer.fact(result['result']['results'][0]['data_version'])['payload']['period']=='2026-05-01'
    assert primary['calendar_version'] is None


def test_lease_expiring_inside_commit_rolls_back_before_any_completion(governed,monkeypatch):
    source,request,clock,_,_,policy,_=governed;service=manager(source.writer,source)
    job=service.create(specification(request,policy_version=policy),idempotency_key='late-commit')
    original=source.writer.write
    def late(*args,**kw):
        result=original(*args,**kw);clock.now+=timedelta(seconds=121);return result
    monkeypatch.setattr(source.writer,'write',late)
    with pytest.raises(WriterError,match='SYNC_LEASE_STALE'):service.run(job,lambda work:[request])
    assert service.inspect(job)['status']=='failed' and not service.coverage(job)['coverage_complete']
    assert source.writer.audit(source.writer.preview(request)['logical_key'])['counts']['facts']==0


def test_configured_control_store_without_parent_establishes_real_http_context(system,tmp_path,monkeypatch):
    import requests
    from app.services.data_source_control import RequestController,RequestPolicy
    from app.services.akshare_utils import ensure_controlled_http
    from app.services.governance_macro import macro_contract,capture_fred
    from app.services.governance_writer import FactWriter
    from tests.data_governance.test_macro_releases import fred_capture
    from app.services.governance_macro import MacroSyncService
    ensure_controlled_http();control=RequestController(tmp_path/'control.sqlite');control.set_provider('fred',RequestPolicy())
    control.set_endpoint('fred:UNRATE','fred',RequestPolicy(),transport='requests',allowed_hosts=('fred.stlouisfed.org',))
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB',control.path)
    contract=macro_contract('fred:UNRATE',evidence=b'SYNTHETIC FRED field contract');writer=FactWriter(system[0].engine,contracts=[contract],clock=system[2])
    options=dict(product='fred:UNRATE',contract_version=contract.version,start='2026-05-01',end_exclusive='2026-06-01',source_uri='https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE')
    request=MacroSyncService(writer).requests(fred_capture(),**options,batch_id='template')[0]
    executor,policy=policy_for(writer,contract);service=manager(writer,executor)
    job=service.create(specification(request,adapter='macro',options=options,policy_version=policy),idempotency_key='control-without-parent')
    hits=[]
    def send(adapter,request,**kw):
        hits.append(request.url);response=requests.Response();response.status_code=200;response.url=request.url
        response._content=b'observation_date,UNRATE\n2026-05-01,4.2\n';response._content_consumed=True;return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter,'send',send)
    result=service.run(job,lambda work:capture_fred('fred:UNRATE'))
    assert result['status']=='done' and len(hits)==1 and result['coverage']['coverage_complete']
    calls=control.statistics(include_records=True)['calls']
    assert len(calls)==1 and calls[0]['task_id']=='sync:'+job
