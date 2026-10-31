"""M07.6 candidate macro/release facts; synthetic documents are not live certification."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from app.services.governance_fact_contracts import WriterError, canonical, digest
from app.services.governance_macro import (FRED_PRODUCTS, OFFICIAL_PRODUCTS, MacroSyncService, macro_contract,
    capture_fred, reviewed_release_capture, reviewed_fred_vintage_capture)
from app.services.governance_releases import (AnnouncementSyncService, announcement_contract,
    capture_public_release, PUBLIC_RELEASES)
from app.services.governance_writer import FactWriter
from tests.test_governance_writer import system, candidate, annual_page, approval

pytestmark = pytest.mark.whitebox


@pytest.fixture
def macro(system):
    base, _, clock, _ = system
    contracts = {p: macro_contract(p, evidence=b'SYNTHETIC explicit source/unit/definition review')
                 for p in [*OFFICIAL_PRODUCTS, *('fred:' + s for s in FRED_PRODUCTS)]}
    writer = FactWriter(base.engine, contracts=list(contracts.values()), clock=clock)
    return MacroSyncService(writer), contracts, clock


def csv_body(series, rows):
    return ('observation_date,' + series + '\n' + ''.join(on + ',' + value + '\n' for on, value in rows)).encode()


def fred_capture(series='UNRATE', rows=None, **vintage):
    body = csv_body(series, rows or [('2026-04-01', '4.2'), ('2026-05-01', '4.3')])
    if vintage:
        return reviewed_fred_vintage_capture('fred:' + series, body, publication_precision='exact',
            release_evidence=b'SYNTHETIC actual archival release reference', review_evidence=b'SYNTHETIC archive/vintage review', **vintage)
    return canonical({'format': 'macro-capture-v1', 'product': 'fred:' + series, 'kind': 'current_csv',
                      'body_b64': base64.b64encode(body).decode(), 'release': None}).encode()


def fred_requests(macro, capture=None, series='UNRATE', **kwargs):
    service, contracts, _ = macro
    return service.requests(capture or fred_capture(series), product='fred:' + series,
        contract_version=contracts['fred:' + series].version, start='2026-05-01', end_exclusive='2026-06-01',
        source_uri='https://fred.stlouisfed.org/graph/fredgraph.csv?id=' + series, batch_id='fred-A', **kwargs)


def official_row(product, **kwargs):
    _, _, frequency, unit, _, _ = OFFICIAL_PRODUCTS[product]
    return {'period': '2026-05-01', 'frequency': frequency, 'native_unit': unit, 'value': '2.5',
        'vintage': '2026-05-release-1', 'publication_precision': 'exact', 'published_at': '2026-05-20T10:00:00+08:00',
        'publication_date': None, 'release_kind': 'observation'} | kwargs


def official_requests(macro, product='nbs_cpi', records=None, **kwargs):
    service, contracts, _ = macro; provider = OFFICIAL_PRODUCTS[product][0]
    uri = 'https://www.' + PUBLIC_RELEASES[provider][0] + '/synthetic-official-release'
    raw = reviewed_release_capture(product, b'SYNTHETIC supplier document body', records=records or [official_row(product)],
        review_evidence=b'SYNTHETIC explicit extraction/unit/time review', source_uri=uri)
    return service.requests(raw, product=product, contract_version=contracts[product].version,
        start='2026-05-01', end_exclusive='2026-06-01', source_uri=uri, batch_id='official-A', **kwargs)


@pytest.mark.parametrize('series,rows,expected', [
    ('CPIAUCSL', [('2025-05-01', '100'), ('2026-05-01', '105')], '5.00000000'),
    ('CPILFESL', [('2026-04-01', '100'), ('2026-05-01', '101')], '1.00000000'),
    ('PPIACO', [('2025-05-01', '100'), ('2026-05-01', '99')], '-1.00000000'),
    ('INDPRO', [('2025-05-01', '100'), ('2026-05-01', '110')], '10.00000000'),
    ('PAYEMS', [('2026-04-01', '10000'), ('2026-05-01', '10123')], '12.30000000'),
    ('UNRATE', [('2026-05-01', '4.2')], '4.20000000'),
])
def test_six_fred_products_preserve_existing_transforms_and_units(macro, series, rows, expected):
    service, _, _ = macro; request = fred_requests(macro, fred_capture(series, rows), series)[0]
    written = service.writer.write(request, approval(service.writer, request))
    fact = service.writer.fact(written['data_version'])
    assert fact['payload']['value'] == expected
    assert fact['payload']['unit'] == FRED_PRODUCTS[series][2]
    assert fact['published_at'] is None and fact['public_available_at'] is None
    assert service.writer.evidence(fact['raw_hash'])['raw_bytes'] == request['raw_response']
    assert request['calendar_version'] is None and request['instrument_version'] is None


@pytest.mark.parametrize('series,rows', [
    ('CPILFESL', [('2026-03-01', '100'), ('2026-05-01', '102')]),
    ('CPILFESL', [('2026-04-01', '.'), ('2026-05-01', '102')]),
    ('CPILFESL', [('2026-04-01', '100'), ('2026-05-01', '.')]),
    ('CPILFESL', [('2026-04-01', '0'), ('2026-05-01', '102')]),
    ('PAYEMS', [('2026-03-01', '10000'), ('2026-05-01', '10200')]),
    ('CPIAUCSL', [('2025-04-01', '100'), ('2025-06-01', '101'), ('2026-05-01', '102')]),
])
def test_missing_month_missing_value_or_zero_denominator_remains_null(macro, series, rows):
    service, _, _ = macro; request = fred_requests(macro, fred_capture(series, rows), series)[0]
    assert request['wire_record']['value'] is None
    staged = service.writer.write(request)
    assert not staged['accepted'] and service.writer.fact(staged['data_version'])['payload']['value'] is None
    assert service.writer.current('macro', staged['logical_key']) is None


@pytest.mark.parametrize('product', list(OFFICIAL_PRODUCTS))
def test_each_official_product_has_frozen_source_frequency_units_and_byte_evidence(macro, product):
    service, contracts, _ = macro; request = official_requests(macro, product)[0]
    result = service.writer.write(request, approval(service.writer, request))
    fact = service.writer.fact(result['data_version']); spec = OFFICIAL_PRODUCTS[product]
    assert fact['payload']['value'] == ('250000000.00000000' if spec[3] == 'CNY_100M' else '2.50000000')
    assert fact['payload']['unit'] == ('CNY' if spec[3] == 'CNY_100M' else spec[3])
    assert fact['payload']['frequency'] == spec[2] and fact['source_tag'].startswith(spec[0] + ':')
    assert 'vintage' not in request['key'] and fact['source_revision_token'] == 'vintage:2026-05-release-1'
    assert service.writer.evidence(fact['publication_evidence_hash'])['raw_bytes'] == request['raw_response']
    assert fact['definition_id'] == contracts[product].document['definition_id']


def test_same_lpr_definition_across_official_publishers_does_not_fork_business_key(macro):
    service, contracts, _ = macro
    first = official_requests(macro, 'pboc_lpr_1y')[0]; second = official_requests(macro, 'chinamoney_lpr_1y')[0]
    assert first['key'] == second['key']
    assert service.writer.preview(first)['logical_key'] == service.writer.preview(second)['logical_key']
    assert contracts['pboc_lpr_1y'].version != contracts['chinamoney_lpr_1y'].version
    assert service.writer.preview(first)['data_version'] != service.writer.preview(second)['data_version']


@pytest.mark.parametrize('change', [
    {'frequency': 'daily'}, {'native_unit': 'CNY'}, {'period': '2026-05-02'},
    {'value': 'NaN'}, {'value': True}, {'publication_precision': 'unknown', 'published_at': '2026-05-20T10:00:00Z'},
    {'published_at': '2026-04-30T10:00:00+08:00'}, {'release_kind': 'fake'},
])
def test_official_release_rejects_ambiguous_units_periods_values_and_times(macro, change):
    with pytest.raises(WriterError): official_requests(macro, records=[official_row('nbs_cpi', **change)])


@pytest.mark.parametrize('part', ['value', 'time', 'period', 'vintage', 'namespace', 'raw'])
def test_writer_revalidates_prepared_macro_instead_of_trusting_adapter_payload(macro, part):
    service, _, _ = macro; request = official_requests(macro)[0]
    if part == 'value': request['wire_record']['value'] = '999'
    elif part == 'time': request['time']['published_at'] = '2026-05-21T10:00:00+08:00'
    elif part == 'period': request['key']['period'] = request['wire_record']['period'] = '2026-05-02'
    elif part == 'vintage': request['revision']['token'] = 'vintage:invented'
    elif part == 'namespace': request['calendar_version'] = 'inapplicable'
    else: request['raw_response'] = request['raw_response'] + b' '
    with pytest.raises(WriterError): service.writer.write(request)


def test_current_fred_has_system_knowledge_only_and_no_historical_publication_claim(macro):
    service, _, clock = macro; request = fred_requests(macro)[0]
    result = service.writer.write(request, approval(service.writer, request))
    options = dict(key=request['key'], as_of='2026-06-02T10:00:00Z')
    assert service.read_as_of(**options, knowledge_mode='system_known')['data_version'] == result['data_version']
    assert service.read_as_of(**options, knowledge_mode='public_known')['status'] == 'PUBLICATION_UNKNOWN'
    assert service.read_as_of(key=request['key'], as_of='2026-05-25T00:00:00Z', knowledge_mode='public_known')['status'] == 'missing'
    assert service.read_as_of(key=request['key'], as_of='2026-05-25T00:00:00Z', knowledge_mode='system_known')['status'] == 'missing'
    request['time'].update(publication_precision='date', publication_date='2026-05-01', publication_evidence=b'invented')
    with pytest.raises(WriterError, match='MACRO_RELEASE_UNBOUND'): service.writer.preview(request)


def test_vintage_correction_and_withdrawal_preserve_key_old_fact_and_bitemporal_reads(macro):
    service, _, clock = macro
    original = fred_requests(macro, fred_capture(vintage='v1', published_at='2026-05-10T08:30:00-04:00'))[0]
    first = service.writer.write(original, approval(service.writer, original)); old = service.writer.fact(first['data_version'])
    clock.advance()
    correction = fred_requests(macro, fred_capture(rows=[('2026-05-01', '4.4')], vintage='v2', published_at='2026-05-20T08:30:00-04:00'),
        revisions={'2026-05-01': {'kind': 'correction', 'token': 'vintage:v2', 'order': None, 'order_evidence': None, 'supersedes_version': first['data_version']}})[0]
    second = service.writer.write(correction, approval(service.writer, correction, revision=1, event='T11'))
    assert original['key'] == correction['key'] and second['data_version'] != first['data_version']
    args = dict(key=original['key'], knowledge_mode='public_known')
    assert service.read_as_of(**args, as_of='2026-05-15T00:00:00Z')['data_version'] == first['data_version']
    assert service.read_as_of(**args, as_of='2026-05-25T00:00:00Z')['data_version'] == second['data_version']
    assert service.read_as_of(key=original['key'], knowledge_mode='system_known', as_of='2026-06-02T10:00:00Z')['data_version'] == first['data_version']
    clock.advance()
    withdrawn = fred_requests(macro, fred_capture(rows=[('2026-05-01', '.')], vintage='v3',
        published_at='2026-05-30T08:30:00-04:00', release_kind='withdrawal'), revisions={'2026-05-01': {
            'kind': 'withdrawal', 'token': 'vintage:v3', 'order': None, 'order_evidence': None, 'supersedes_version': second['data_version']}})[0]
    third = service.writer.write(withdrawn, approval(service.writer, withdrawn, revision=2, event='T11'))
    assert service.read_as_of(**args, as_of='2026-06-01T00:00:00Z') == {'status': 'withdrawn', 'data_version': third['data_version']}
    assert service.writer.fact(first['data_version']) == old


def test_a_current_fred_capture_cannot_be_used_as_withdrawal_evidence(macro):
    service, _, _ = macro; original = fred_requests(macro)[0]
    first = service.writer.write(original, approval(service.writer, original))
    original['revision'].update(kind='withdrawal', supersedes_version=first['data_version'])
    original['wire_record'] = {'retraction_reason': 'unproven caller assertion'}
    with pytest.raises(WriterError, match='MACRO_WITHDRAWAL_UNBOUND'): service.writer.preview(original)


def test_unordered_macro_revision_conflicts_until_explicit_arbitration(macro):
    service, _, clock = macro; request = official_requests(macro)[0]
    first = service.writer.write(request, approval(service.writer, request)); clock.advance()
    revision = {'kind': 'correction', 'token': 'vintage:second', 'order': None, 'order_evidence': None, 'supersedes_version': first['data_version']}
    changed = official_requests(macro, records=[official_row('nbs_cpi', value='2.6', vintage='second')], revisions={'2026-05-01': revision})[0]
    blocked = service.writer.write(changed, approval(service.writer, changed, revision=1, event='T09'))
    assert not blocked['accepted'] and blocked['outcome'] == 'conflict'
    assert service.writer.current('macro', first['logical_key'])['data_version'] == first['data_version']
    clock.advance()
    accepted = service.writer.write(changed, approval(service.writer, changed, revision=1, event='T11'))
    assert accepted['accepted'] and accepted['data_version'] != first['data_version']


def test_equivalent_current_csv_bytes_append_observation_without_new_fact_or_decision(macro):
    service, _, clock = macro; request = fred_requests(macro, fred_capture(rows=[('2026-05-01', '4.2')]))[0]
    command = approval(service.writer, request); first = service.writer.write(request, command); clock.advance()
    same = fred_requests(macro, fred_capture(rows=[('2026-05-01', '4.200')]))[0]
    same['batch_id'] = 'fred-replay'
    second = service.writer.write(same, command)
    assert first == second
    assert service.writer.audit(first['logical_key'])['counts'] == {'facts': 1, 'decisions': 1, 'receipts': 1, 'observations': 2, 'outbox': 1, 'history': 1}
    assert service.writer.evidence(digest(same['raw_response']))['raw_bytes'] == same['raw_response']


def test_unknown_public_time_in_new_current_revision_blocks_old_vintage_as_current(macro):
    service, _, clock = macro
    first = fred_requests(macro, fred_capture(vintage='v1', published_at='2026-05-10T08:30:00-04:00'))[0]
    v1 = service.writer.write(first, approval(service.writer, first)); clock.advance()
    changed = fred_requests(macro, fred_capture(rows=[('2026-05-01', '4.5')]), revisions={'2026-05-01': {
        'kind': 'correction', 'token': None, 'order': None, 'order_evidence': None, 'supersedes_version': v1['data_version']}})[0]
    service.writer.write(changed, approval(service.writer, changed, revision=1, event='T11'))
    assert service.read_as_of(key=first['key'], knowledge_mode='public_known', as_of='2026-05-15T00:00:00Z')['data_version'] == v1['data_version']
    assert service.read_as_of(key=first['key'], knowledge_mode='public_known', as_of='2026-06-02T10:00:02Z')['status'] == 'PUBLICATION_UNKNOWN'


def test_date_only_macro_release_is_conservative_and_not_moved_to_trading_date(macro):
    service, _, _ = macro
    request = official_requests(macro, records=[official_row('nbs_cpi', publication_precision='date',
        published_at=None, publication_date='2026-05-31')])[0]
    result = service.writer.write(request, approval(service.writer, request))
    fact = service.writer.fact(result['data_version'])
    assert fact['public_available_at'] == datetime(2026, 5, 31, 16)
    assert fact['event_date'].isoformat() == '2026-05-01'
    assert service.read_as_of(key=request['key'], as_of='2026-05-31T15:59:59Z', knowledge_mode='public_known')['status'] == 'missing'
    assert service.read_as_of(key=request['key'], as_of='2026-05-31T16:00:00Z', knowledge_mode='public_known')['status'] == 'pass'


def test_later_proven_vintage_resolves_unknown_revision_without_poisoning_entire_chain(macro):
    service, _, clock = macro
    first = fred_requests(macro, fred_capture(vintage='v1', published_at='2026-05-10T08:30:00-04:00'))[0]
    v1 = service.writer.write(first, approval(service.writer, first)); clock.advance()
    current = fred_requests(macro, fred_capture(rows=[('2026-05-01', '4.5')]), revisions={'2026-05-01': {
        'kind': 'correction', 'token': None, 'order': None, 'order_evidence': None, 'supersedes_version': v1['data_version']}})[0]
    v2 = service.writer.write(current, approval(service.writer, current, revision=1, event='T11'))
    clock.now = datetime(2026, 6, 4, 10, tzinfo=timezone.utc)
    proven = fred_requests(macro, fred_capture(rows=[('2026-05-01', '4.6')], vintage='v3', published_at='2026-06-03T08:30:00-04:00'),
        revisions={'2026-05-01': {'kind': 'correction', 'token': 'vintage:v3', 'order': None, 'order_evidence': None, 'supersedes_version': v2['data_version']}})[0]
    v3 = service.writer.write(proven, approval(service.writer, proven, revision=2, event='T11'))
    args = dict(key=first['key'], knowledge_mode='public_known')
    assert service.read_as_of(**args, as_of='2026-05-15T00:00:00Z')['data_version'] == v1['data_version']
    assert service.read_as_of(**args, as_of='2026-06-02T12:00:00Z')['status'] == 'PUBLICATION_UNKNOWN'
    assert service.read_as_of(**args, as_of='2026-06-03T13:00:00Z')['data_version'] == v3['data_version']
    assert service.read_as_of(**args, as_of='2026-06-05T00:00:00Z')['payload']['value'] == '4.60000000'


def test_macro_staging_is_atomic_and_empty_does_not_grant_coverage(macro, monkeypatch):
    service, contracts, _ = macro
    capture = fred_capture(rows=[('2026-04-01', '4.2'), ('2026-05-01', '4.3')])
    options = dict(product='fred:UNRATE', contract_version=contracts['fred:UNRATE'].version,
        start='2026-04-01', end_exclusive='2026-06-01', source_uri='https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE', batch_id='atomic')
    requests = service.requests(capture, **options); first = service.writer.preview(requests[0])
    real = service.writer.write; calls = []
    def fail_second(request, **kwargs):
        calls.append(True)
        if len(calls) == 2: raise RuntimeError('injected commit-path failure')
        return real(request, **kwargs)
    monkeypatch.setattr(service.writer, 'write', fail_second)
    with pytest.raises(RuntimeError): service.ingest(capture, **options)
    assert service.writer.audit(first['logical_key'])['counts']['facts'] == 0
    monkeypatch.setattr(service.writer, 'write', real)
    options.update(start='2026-07-01', end_exclusive='2026-08-01')
    empty = service.ingest(capture, **options)
    assert empty['status'] == 'empty' and empty['coverage_complete'] is False


@pytest.fixture
def announcements(system):
    base, _, clock, _ = system
    contracts = {p: announcement_contract(p, evidence=b'SYNTHETIC official body field mapping') for p in ['SSE', 'SZSE', 'CNINFO']}
    writer = FactWriter(base.engine, contracts=list(contracts.values()), clock=clock)
    return AnnouncementSyncService(writer), contracts, clock


def notice(announcements, authority='CNINFO', body=b'SYNTHETIC official Sunday notice', **kwargs):
    service, contracts, _ = announcements
    options = dict(authority=authority, announcement_id='A001', contract_version=contracts[authority].version,
        source_uri='https://www.' + PUBLIC_RELEASES[authority][0] + '/synthetic-A001', batch_id='notice-A',
        publication_precision='exact', published_at='2026-05-31T10:00:00+08:00')
    return service.request(body, **(options | kwargs))


@pytest.mark.parametrize('authority', ['SSE', 'SZSE', 'CNINFO'])
def test_official_announcement_keeps_sunday_body_hash_and_exact_publication(announcements, authority):
    service, _, _ = announcements; request = notice(announcements, authority)
    result = service.writer.write(request, approval(service.writer, request)); fact = service.writer.fact(result['data_version'])
    assert fact['event_date'].isoformat() == '2026-05-31' and fact['event_date'].weekday() == 6
    assert fact['payload']['content_hash'] == digest(request['raw_response'])
    assert service.writer.evidence(fact['raw_hash'])['raw_bytes'] == request['raw_response']
    assert service.read_as_of(key=request['key'], knowledge_mode='public_known', as_of='2026-05-31T02:00:00Z')['status'] == 'pass'
    assert service.read_as_of(key=request['key'], knowledge_mode='system_known', as_of='2026-05-31T02:00:00Z')['status'] == 'missing'


def test_announcement_revised_body_and_withdrawal_retain_stable_id_and_old_result(announcements):
    service, _, clock = announcements; original = notice(announcements)
    first = service.writer.write(original, approval(service.writer, original)); old = service.writer.fact(first['data_version'])
    clock.advance()
    corrected = notice(announcements, body=b'SYNTHETIC corrected original notice A001', published_at='2026-06-01T10:00:00+08:00',
        revision={'kind': 'correction', 'token': 'correction-1', 'order': None, 'order_evidence': None, 'supersedes_version': first['data_version']})
    second = service.writer.write(corrected, approval(service.writer, corrected, revision=1, event='T11'))
    clock.advance()
    withdrawn = notice(announcements, body=b'SYNTHETIC explicit withdrawal A001', published_at='2026-06-02T10:00:00+08:00',
        event_date='2026-05-31', retraction_reason='official notice withdrawn',
        revision={'kind': 'withdrawal', 'token': 'withdrawal-1', 'order': None, 'order_evidence': None, 'supersedes_version': second['data_version']})
    third = service.writer.write(withdrawn, approval(service.writer, withdrawn, revision=2, event='T11'))
    assert original['key'] == corrected['key'] == withdrawn['key']
    args = dict(key=original['key'], knowledge_mode='public_known')
    assert service.read_as_of(**args, as_of='2026-05-31T04:00:00Z')['data_version'] == first['data_version']
    assert service.read_as_of(**args, as_of='2026-06-01T04:00:00Z')['data_version'] == second['data_version']
    assert service.read_as_of(**args, as_of='2026-06-02T04:00:00Z') == {'status': 'withdrawn', 'data_version': third['data_version']}
    assert service.writer.fact(first['data_version']) == old


def test_announcement_date_only_staging_and_authority_are_fail_closed(announcements):
    service, _, _ = announcements
    request = notice(announcements, publication_precision='date', published_at=None, publication_date='2026-05-31')
    staged = service.ingest([request]); assert staged['status'] == 'staged' and not staged['coverage_complete']
    assert service.read_as_of(key=request['key'], knowledge_mode='system_known', as_of='2026-06-02T10:00:00Z')['status'] == 'missing'
    with pytest.raises(WriterError, match='RELEASE_AUTHORITY_UNVERIFIED'):
        notice(announcements, source_uri='https://www.cninfo.com.cn.evil.invalid/A001')
    with pytest.raises(WriterError, match='ANNOUNCEMENT_PUBLICATION_UNKNOWN'):
        notice(announcements, publication_precision='unknown', published_at=None)
    with pytest.raises(WriterError, match='ANNOUNCEMENT_CONTENT_UNBOUND'):
        request['wire_record']['content_hash'] = '0' * 64; service.writer.preview(request)


@pytest.mark.parametrize('part', ['time', 'raw', 'authority'])
def test_announcement_tombstone_cannot_bypass_source_or_publication_evidence(announcements, part):
    service, _, _ = announcements; request = notice(announcements)
    first = service.writer.write(request, approval(service.writer, request))
    request['revision'].update(kind='withdrawal', token='withdrawal', supersedes_version=first['data_version'])
    request['wire_record'] = {'retraction_reason': 'source withdrawal'}
    if part == 'time': request['time'].update(publication_precision='unknown', published_at=None, publication_evidence=None)
    elif part == 'raw': request['time']['publication_evidence'] = b'not this source body'
    else: request['source_uri'] = 'https://www.cninfo.com.cn.evil.invalid/'
    with pytest.raises(WriterError): service.writer.preview(request)


class Response:
    status_code = 200
    def __init__(self, body): self.content = body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_content(self, chunk_size): yield self.content


def test_actual_fred_raw_entry_reuses_controller_without_key_and_keeps_supplier_bytes(tmp_path, monkeypatch):
    from tests.test_data_source_execution import controller
    import requests
    c = controller(tmp_path, endpoint='fred:UNRATE'); calls = []
    body = csv_body('UNRATE', [('2026-05-01', '4.2')])
    def get(url, **kwargs): calls.append((url, kwargs)); return Response(body)
    monkeypatch.setattr(requests, 'get', get)
    with c.scope('job'): capture = capture_fred('fred:UNRATE')
    doc = json.loads(capture)
    assert base64.b64decode(doc['body_b64']) == body and doc['release'] is None
    assert calls[0][0] == 'https://fred.stlouisfed.org/graph/fredgraph.csv'
    assert calls[0][1]['params'] == {'id': 'UNRATE'} and not calls[0][1]['allow_redirects']
    assert c.statistics()['totals']['attempts'] == 1


@pytest.mark.parametrize('provider', [p for p in PUBLIC_RELEASES if p != 'BSE'])
def test_each_public_release_download_traverses_its_registered_endpoint(tmp_path, monkeypatch, provider):
    from tests.test_data_source_execution import controller
    import requests
    host, endpoint = PUBLIC_RELEASES[provider]; c = controller(tmp_path, endpoint=endpoint)
    calls = []
    def get(url, **kwargs): calls.append(kwargs); return Response(b'SYNTHETIC public source bytes')
    monkeypatch.setattr(requests, 'get', get)
    with c.scope('job'): body = capture_public_release(provider, 'https://www.' + host + '/synthetic')
    assert body == b'SYNTHETIC public source bytes' and calls[0]['stream'] and calls[0]['allow_redirects'] is False
    assert c.statistics()['totals']['attempts'] == 1


@pytest.mark.parametrize('endpoint,operation', [
    ('fred:UNRATE', lambda: capture_fred('fred:UNRATE')),
    ('PLAN:NBS_direct', lambda: capture_public_release('NBS', 'https://www.stats.gov.cn/synthetic')),
    ('PLAN:CNINFO_web', lambda: capture_public_release('CNINFO', 'https://www.cninfo.com.cn/synthetic')),
])
def test_disabled_or_unadmitted_sources_do_not_contact_supplier_or_fallback(tmp_path, monkeypatch, endpoint, operation):
    from tests.test_data_source_execution import controller
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    import requests
    c = controller(tmp_path, endpoint=endpoint)
    monkeypatch.setattr(requests, 'get', lambda *a, **k: pytest.fail('policy denied before network'))
    c.set_provider('provider', RequestPolicy(enabled=False), expected_version=1)
    with c.scope('job'), pytest.raises(PolicyDenied, match='DISABLED'): operation()
    c.set_provider('provider', RequestPolicy(enabled=True), expected_version=2)
    monkeypatch.setenv('DATA_SOURCE_EXECUTION_MODE', 'production')
    with c.scope('job'), pytest.raises(PolicyDenied, match='PRODUCTION_NOT_ADMITTED'): operation()
    assert c.statistics()['totals']['attempts'] == 0


@pytest.mark.parametrize('failure', ['empty', 'redirect', 'oversize', 'cancelled', 'evil_host'])
def test_release_transport_rejects_empty_redirect_size_cancel_and_wrong_domain(monkeypatch, failure):
    import requests
    body = b'' if failure == 'empty' else b'x' * (16 * 1024 * 1024 + 1) if failure == 'oversize' else b'synthetic'
    response = Response(body)
    if failure == 'redirect': response.status_code = 302
    monkeypatch.setattr(requests, 'get', lambda *a, **k: response)
    uri = 'https://www.stats.gov.cn.evil.invalid/' if failure == 'evil_host' else 'https://www.stats.gov.cn/synthetic'
    with pytest.raises(WriterError): capture_public_release('NBS', uri, should_cancel=lambda: failure == 'cancelled')


@pytest.mark.parametrize('body', [b'observation_date,OTHER\n2026-05-01,4.2\n',
    b'observation_date,UNRATE\n2026-05-01,4.2\n2026-05-01,4.3\n', b'observation_date,UNRATE\n2026-05-02,4.2\n',
    b'observation_date,UNRATE\n2026-05-01,NaN\n', b'<html>blocked</html>'])
def test_bad_fred_csv_never_becomes_valid_empty_coverage(macro, body):
    capture = canonical({'format': 'macro-capture-v1', 'product': 'fred:UNRATE', 'kind': 'current_csv',
        'body_b64': base64.b64encode(body).decode(), 'release': None}).encode()
    with pytest.raises(WriterError): fred_requests(macro, capture)


def test_bse_is_still_plan_only_and_does_not_gain_contract_or_network_access(tmp_path, monkeypatch):
    import requests
    from app.services.data_source_control import PolicyDenied, RequestController, RequestPolicy
    from app.services.akshare_utils import AkshareApiDisabledError
    with pytest.raises(WriterError, match='SOURCE_OUT_OF_RELEASE'):
        announcement_contract('BSE', evidence=b'SYNTHETIC cannot approve out of release')
    c = RequestController(tmp_path / 'blocked-bse.sqlite'); c.set_provider('bse', RequestPolicy())
    with pytest.raises(PolicyDenied, match='ENDPOINT_OUT_OF_RELEASE'):
        c.set_endpoint('PLAN:BSE', 'bse', RequestPolicy())
    monkeypatch.setattr(requests, 'get', lambda *a, **k: pytest.fail('BSE plan cannot contact network'))
    with pytest.raises(AkshareApiDisabledError): capture_public_release('BSE', 'https://www.bse.cn/synthetic')


@pytest.mark.parametrize('provider', [p for p in PUBLIC_RELEASES if p != 'BSE'])
def test_interface_management_shows_candidate_implementation_without_production_permission(provider):
    from app.services.data_source_management import metadata
    value = metadata(PUBLIC_RELEASES[provider][1], registered=True)
    assert value['implementation_status'] == 'implemented' and value['in_current_release']
    assert value['contract_status'] == 'candidate_reviewed_extraction_not_supplier_verified'
    assert value['production_enabled'] is False and value['production_request_allowed'] is False
    assert value['admission_status'] == 'not_reviewed'


@pytest.mark.parametrize('product', ['NBS', 'FRED'])
def test_real_requests_wrapper_binds_official_host_and_counts_one_physical_request(tmp_path, monkeypatch, product):
    import requests
    from app.services.akshare_utils import ensure_controlled_http
    from app.services.data_source_control import RequestController, RequestPolicy
    ensure_controlled_http()
    endpoint = 'fred:UNRATE' if product == 'FRED' else PUBLIC_RELEASES[product][1]
    host = 'fred.stlouisfed.org' if product == 'FRED' else 'www.stats.gov.cn'
    controller = RequestController(tmp_path / 'physical.sqlite'); controller.set_provider('official', RequestPolicy())
    controller.set_endpoint(endpoint, 'official', RequestPolicy(), transport='requests', allowed_hosts=(host,))
    controller.start_task('job', lane='incremental', max_requests=1, timeout_s=15)
    hits = []; body = csv_body('UNRATE', [('2026-05-01', '4.2')]) if product == 'FRED' else b'SYNTHETIC official body'
    def send(adapter, request, **kwargs):
        hits.append(request.url); response = requests.Response(); response.status_code = 200
        response._content = body; response._content_consumed = True; response.url = request.url
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    with controller.scope('job'):
        capture = capture_fred('fred:UNRATE') if product == 'FRED' else capture_public_release(product, 'https://' + host + '/synthetic')
    assert len(hits) == 1 and controller.statistics()['totals']['attempts'] == 1
    assert base64.b64decode(json.loads(capture)['body_b64']) == body if product == 'FRED' else capture == body
