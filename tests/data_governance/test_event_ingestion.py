"""M06.2: official announcement-linked event facts through public candidate seams."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from tests.test_governance_writer import system, candidate, annual_page, approval

pytestmark = pytest.mark.whitebox


def _field(name, unit):
    return {'wire_field': name, 'native_unit': unit, 'normalized_unit': unit, 'scale': '1', 'offset': '0'}


@pytest.fixture
def events(system):
    from app.services.governance_writer import FactWriter, WriterContract
    from app.services.governance_fact_contracts import digest

    old, template, clock, _ = system
    definitions = {
        'announcement': ('announcement/core', {'authority': 'text', 'announcement_id': 'text', 'content_hash': 'hash',
            'published_at': 'time', 'publication_date': 'date'}),
        'share_capital': ('share_capital/core', {'share_class': 'text', 'share_definition': 'text',
            'total_shares': 'share', 'float_shares': 'share', 'free_float_shares': 'share',
            'announcement_id': 'text', 'announcement_data_version': 'hash', 'event_type': 'text', 'change_reason': 'text'}),
        'corporate_action': ('corporate_action/core', {'event_type': 'text', 'cash_dividend': 'CNY',
            'stock_ratio': 'x', 'rights_ratio': 'x', 'rights_price': 'CNY', 'record_date': 'date',
            'split_ratio': 'x', 'announcement_id': 'text', 'announcement_data_version': 'hash'}),
    }
    contracts = {dataset: WriterContract.profile(dataset=dataset, profile=profile, provider='SSE',
        endpoint='disclosure', asset_type='stock', instrument_family='sh_main',
        definition={'schema': dataset+'-official-v1'}, fields={name: _field(name, unit) for name, unit in fields.items()},
        evidence=b'SYNTHETIC isolated SSE field/unit mapping', publication_timezone='Asia/Shanghai',
        revision_order='integer' if dataset in {'corporate_action', 'share_capital'} else None)
        for dataset, (profile, fields) in definitions.items()}
    writer = FactWriter(old.engine, contracts=list(contracts.values()), clock=clock)
    raw = b'SYNTHETIC SSE notice A001 content and public timestamp 2026-06-02T16:00+08:00'
    ann = deepcopy(template)
    ann.update(dataset='announcement', key={'authority': 'SSE', 'announcement_id': 'A001'},
        contract_version=contracts['announcement'].version, profile='announcement/core',
        instrument_version=None, calendar_version=None, raw_response=raw,
        source_uri='https://www.sse.com.cn/disclosure/A001',
        wire_record={'authority': 'SSE', 'announcement_id': 'A001', 'content_hash': digest(raw),
                     'published_at': '2026-06-02T16:00:00+08:00'}, flags={}, gate_results={})
    ann['time'] = {'event_date': '2026-06-02', 'publication_precision': 'exact',
        'published_at': '2026-06-02T16:00:00+08:00', 'publication_date': None,
        'valid_from': '2026-06-02', 'valid_to': '2026-06-03', 'publication_evidence': raw}
    notice = writer.write(ann, approval(writer, ann))['data_version']
    base = deepcopy(template)
    base.update(dataset='corporate_action', key={'instrument_id': template['key']['instrument_id'], 'authority_event_id': 'SSE:A001:1'},
        contract_version=contracts['corporate_action'].version, profile='corporate_action/core',
        calendar_version=None, raw_response=raw, source_uri='https://www.sse.com.cn/disclosure/A001',
        wire_record={'event_type': 'stock_dividend', 'cash_dividend': '0', 'stock_ratio': '1',
            'rights_ratio': '0', 'rights_price': None, 'record_date': '2026-06-01', 'split_ratio': None,
            'announcement_id': 'A001', 'announcement_data_version': notice}, flags={}, gate_results={})
    base['time'] = {'event_date': '2026-06-02', 'publication_precision': 'exact',
        'published_at': '2026-06-02T16:00:00+08:00', 'publication_date': None,
        'valid_from': '2026-06-02', 'valid_to': '2026-06-03', 'publication_evidence': raw}
    base['revision'] = {'kind': 'original', 'token': 'notice-A001-rev1', 'order': 1,
                        'order_evidence': raw, 'supersedes_version': None}
    return writer, contracts, base, notice, raw, clock


def test_action_is_admitted_with_official_version_and_consumed_by_price_service(events, system):
    from app.services.governance_prices import PriceService, install_price_schema

    writer, _, action, notice, raw, _, = events
    result = writer.write(action, approval(writer, action))
    fact = writer.fact(result['data_version'])
    assert fact['payload']['announcement_data_version'] == notice
    assert fact['payload']['stock_ratio'] == '1.0000000000'
    assert fact['public_available_at'] == datetime(2026, 6, 2, 8)
    price_writer, price_request, _, _ = system
    price_request['wire_record'].update({'开盘': '10', '最高': '10.2', '最低': '9.8', '收盘': '10', '成交量': '1', '成交额': '1000'})
    price_request['gate_results']['P02'] = 'pass'
    raw_version = price_writer.write(price_request, approval(price_writer, price_request))['data_version']
    install_price_schema(writer.engine)
    spec = {'instrument_id': action['key']['instrument_id'], 'instrument_version': action['instrument_version'],
        'calendar_version': action['calendar_version'] or price_request['calendar_version'], 'base_date': '2026-06-02',
        'as_of': '2026-06-02T10:00:00Z', 'knowledge_mode': 'system_known', 'algorithm_version': 'r1-qfq-v1',
        'raw_data_versions': [raw_version], 'event_inputs': [{'data_version': result['data_version'], 'previous_raw_data_version': raw_version}]}
    prices = PriceService(price_writer)
    factor = prices.freeze_factors(spec, evidence=b'SYNTHETIC explicit selected event set')
    assert prices.derive(raw_version, factor)['adj_close'] == '5.00000000'


def test_public_reconstruction_and_system_knowledge_have_distinct_cutoffs(events, system):
    from app.services.governance_prices import PriceService, install_price_schema
    from app.services.governance_fact_contracts import WriterError

    writer, _, action, _, _, _ = events
    event = writer.write(action, approval(writer, action))['data_version']
    price_writer, request, _, _ = system
    request['wire_record'].update({'开盘': '10', '最高': '10.2', '最低': '9.8', '收盘': '10', '成交量': '1', '成交额': '1000'})
    request['gate_results']['P02'] = 'pass'
    request['time'].update(publication_precision='exact', published_at='2026-06-02T16:00:00+08:00',
                           publication_evidence=b'SYNTHETIC public raw publication proof')
    raw = price_writer.write(request, approval(price_writer, request))['data_version']
    install_price_schema(writer.engine)
    spec = {'instrument_id': action['key']['instrument_id'], 'instrument_version': action['instrument_version'],
        'calendar_version': request['calendar_version'], 'base_date': '2026-06-02',
        'as_of': '2026-06-02T09:00:00Z', 'knowledge_mode': 'public_known', 'algorithm_version': 'r1-qfq-v1',
        'raw_data_versions': [raw], 'event_inputs': [{'data_version': event, 'previous_raw_data_version': raw}]}
    prices = PriceService(price_writer)
    assert prices.preview_factors(spec, evidence=b'SYNTHETIC selected public versions')['rows'][0]['factor'] == '0.5000000000000000'
    spec['knowledge_mode'] = 'system_known'
    with pytest.raises(WriterError, match='DEPENDENCY_NOT_KNOWN'):
        prices.preview_factors(spec, evidence=b'SYNTHETIC selected system versions')
    spec.update(knowledge_mode='public_known', as_of='2026-06-02T07:00:00Z')
    with pytest.raises(WriterError, match='DEPENDENCY_NOT_KNOWN'):
        prices.preview_factors(spec, evidence=b'SYNTHETIC selected pre-public versions')


def test_date_only_announcement_uses_next_local_midnight_and_rejects_payload_time_conflicts(events):
    from app.services.governance_fact_contracts import WriterError, digest

    writer, contracts, action, _, _, clock = events
    raw = b'SYNTHETIC SSE notice A003 with date-only publication evidence'
    notice = deepcopy(action)
    notice.update(dataset='announcement', key={'authority': 'SSE', 'announcement_id': 'A003'},
        contract_version=contracts['announcement'].version, profile='announcement/core', instrument_version=None,
        raw_response=raw, source_uri='https://www.sse.com.cn/disclosure/A003',
        wire_record={'authority': 'SSE', 'announcement_id': 'A003', 'content_hash': digest(raw),
                     'published_at': None, 'publication_date': '2026-06-01'})
    notice['time'] = {'event_date': '2026-06-02', 'publication_precision': 'date', 'published_at': None,
        'publication_date': '2026-06-02', 'valid_from': '2026-06-02', 'valid_to': '2026-06-03',
        'publication_evidence': raw}
    notice['revision'] = {'kind': 'original', 'token': None, 'order': None,
                          'order_evidence': None, 'supersedes_version': None}
    with pytest.raises(WriterError, match='ANNOUNCEMENT_PUBLICATION_MISMATCH'):
        writer.preview(notice)
    notice['wire_record']['publication_date'] = '2026-06-02'
    notice['wire_record']['published_at'] = '2026-06-02T16:00:00+08:00'
    with pytest.raises(WriterError, match='ANNOUNCEMENT_PUBLICATION_MISMATCH'):
        writer.preview(notice)
    notice['wire_record']['published_at'] = None
    clock.now = datetime(2026, 6, 2, 16, tzinfo=timezone.utc)
    result = writer.write(notice, approval(writer, notice))
    fact = writer.fact(result['data_version'])
    assert result['accepted'] and fact['published_at'] is None
    assert fact['publication_date'].isoformat() == '2026-06-02'
    assert fact['public_available_at'] == datetime(2026, 6, 2, 16)


@pytest.mark.parametrize('kind,fields,expected', [
    ('cash_dividend', {'cash_dividend': '1', 'stock_ratio': '0'}, '1.00000000'),
    ('rights_issue', {'stock_ratio': '0', 'rights_ratio': '0.5', 'rights_price': '5'}, '0.5000000000'),
    ('split', {'stock_ratio': '1', 'split_ratio': '2'}, '2.0000000000'),
    ('consolidation', {'stock_ratio': '-0.5', 'split_ratio': '0.5'}, '0.5000000000'),
    ('combined', {'cash_dividend': '1', 'stock_ratio': '0.1', 'rights_ratio': '0.2', 'rights_price': '5'}, '0.2000000000'),
])
def test_defined_action_types_are_admitted_with_explicit_parameters(events, kind, fields, expected):
    writer, _, action, _, _, _ = events
    action['wire_record'].update(event_type=kind, **fields)
    result = writer.write(action, approval(writer, action))
    assert result['accepted']
    fact = writer.fact(result['data_version'])
    name = {'cash_dividend': 'cash_dividend', 'rights_issue': 'rights_ratio', 'split': 'split_ratio',
            'consolidation': 'split_ratio', 'combined': 'rights_ratio'}[kind]
    assert fact['payload'][name] == expected


def test_share_capital_keeps_explicit_units_and_business_interval(events):
    writer, contracts, action, notice, raw, _ = events
    capital = deepcopy(action)
    capital.update(dataset='share_capital', profile='share_capital/core', contract_version=contracts['share_capital'].version,
        key={'instrument_id': action['key']['instrument_id'], 'effective_date': '2026-06-02', 'share_class': 'A'},
        wire_record={'share_class': 'A', 'share_definition': 'cn-issued-tradable-freefloat-v1',
            'total_shares': '1000000', 'float_shares': '800000', 'free_float_shares': '700000',
            'announcement_id': 'A001', 'announcement_data_version': notice,
            'event_type': 'stock_dividend', 'change_reason': 'bonus issue'})
    capital['time']['valid_to'] = None
    capital['revision'] = {'kind': 'original', 'token': None, 'order': None,
                           'order_evidence': None, 'supersedes_version': None}
    result = writer.write(capital, approval(writer, capital))
    fact = writer.fact(result['data_version'])
    assert fact['payload']['float_shares'] == '800000.00000000'
    assert fact['valid_from'].isoformat() == '2026-06-02' and fact['valid_to'] is None


def test_official_correction_creates_new_fact_and_preserves_old_version(events):
    from app.services.governance_fact_contracts import digest

    writer, contracts, action, _, _, clock = events
    first = writer.write(action, approval(writer, action))
    old = writer.fact(first['data_version'])
    clock.advance()
    correction_bytes = b'SYNTHETIC SSE amended notice A002 with updated dividend parameters'
    announcement = deepcopy(action)
    announcement.update(dataset='announcement', key={'authority': 'SSE', 'announcement_id': 'A002'},
        contract_version=contracts['announcement'].version, profile='announcement/core',
        instrument_version=None, wire_record={'authority': 'SSE', 'announcement_id': 'A002',
            'content_hash': digest(correction_bytes), 'published_at': '2026-06-02T08:00:01Z'},
        raw_response=correction_bytes, source_uri='https://www.sse.com.cn/disclosure/A002')
    announcement['time'].update(published_at='2026-06-02T16:00:01+08:00', publication_evidence=correction_bytes)
    announcement['revision'] = {'kind': 'original', 'token': None, 'order': None,
                                 'order_evidence': None, 'supersedes_version': None}
    amended_notice = writer.write(announcement, approval(writer, announcement))['data_version']
    corrected = deepcopy(action)
    corrected.update(raw_response=correction_bytes, source_uri='https://www.sse.com.cn/disclosure/A002')
    corrected['wire_record'].update(stock_ratio='0.25', announcement_id='A002', announcement_data_version=amended_notice)
    corrected['time'].update(published_at='2026-06-02T16:00:01+08:00', publication_evidence=correction_bytes)
    corrected['revision'] = {'kind': 'correction', 'token': 'notice-A002-rev2', 'order': 2,
                             'order_evidence': correction_bytes, 'supersedes_version': first['data_version']}
    second = writer.write(corrected, approval(writer, corrected, revision=1, event='T09'))
    assert second['accepted'] and second['data_version'] != first['data_version']
    assert writer.fact(first['data_version']) == old
    assert writer.current('corporate_action', first['logical_key'])['data_version'] == second['data_version']
    assert writer.audit(first['logical_key'])['counts']['history'] == 2


def test_share_capital_correction_is_versioned_without_rewriting_old_interval(events):
    writer, contracts, action, notice, raw, clock = events
    capital = deepcopy(action)
    capital.update(dataset='share_capital', profile='share_capital/core', contract_version=contracts['share_capital'].version,
        key={'instrument_id': action['key']['instrument_id'], 'effective_date': '2026-06-02', 'share_class': 'A'},
        wire_record={'share_class': 'A', 'share_definition': 'cn-issued-tradable-freefloat-v1',
            'total_shares': '100', 'float_shares': '80', 'free_float_shares': '70',
            'announcement_id': 'A001', 'announcement_data_version': notice,
            'event_type': 'stock_dividend', 'change_reason': 'bonus issue'})
    capital['time']['valid_to'] = None
    first = writer.write(capital, approval(writer, capital))
    before = writer.fact(first['data_version'])
    clock.advance()
    corrected = deepcopy(capital)
    corrected['wire_record']['float_shares'] = '75'
    corrected['revision'] = {'kind': 'correction', 'token': 'A001-share-rev2', 'order': 2,
                             'order_evidence': raw, 'supersedes_version': first['data_version']}
    # An ordered correction needs an ordered original; this original had order=1.
    assert before['source_revision_order'] == 1
    second = writer.write(corrected, approval(writer, corrected, revision=1, event='T09'))
    assert second['accepted'] and second['data_version'] != first['data_version']
    assert writer.fact(first['data_version']) == before
    assert writer.current('share_capital', first['logical_key'])['data_version'] == second['data_version']


@pytest.mark.parametrize('change', ['float_above_total', 'free_above_float', 'wrong_definition', 'unknown_event'])
def test_share_capital_definition_and_bounds_are_enforced(events, change):
    from app.services.governance_fact_contracts import WriterError

    writer, contracts, action, notice, _, _ = events
    capital = deepcopy(action)
    capital.update(dataset='share_capital', profile='share_capital/core', contract_version=contracts['share_capital'].version,
        key={'instrument_id': action['key']['instrument_id'], 'effective_date': '2026-06-02', 'share_class': 'A'},
        wire_record={'share_class': 'A', 'share_definition': 'cn-issued-tradable-freefloat-v1',
            'total_shares': '100', 'float_shares': '80', 'free_float_shares': '70',
            'announcement_id': 'A001', 'announcement_data_version': notice,
            'event_type': 'stock_dividend', 'change_reason': 'bonus issue'})
    capital['time']['valid_to'] = None
    capital['revision'] = {'kind': 'original', 'token': None, 'order': None,
                           'order_evidence': None, 'supersedes_version': None}
    if change == 'float_above_total': capital['wire_record']['float_shares'] = '101'
    elif change == 'free_above_float': capital['wire_record']['free_float_shares'] = '81'
    elif change == 'wrong_definition': capital['wire_record']['share_definition'] = 'unknown'
    else: capital['wire_record']['event_type'] = 'mystery'
    with pytest.raises(WriterError): writer.write(capital, approval(writer, capital))


@pytest.mark.parametrize('change', ['missing_announcement', 'wrong_notice', 'wrong_publication', 'unpriced_rights',
    'negative_cash', 'bad_split', 'bad_interval', 'unknown_type', 'unnamespaced_event_id'])
def test_unproved_or_ambiguous_action_cannot_be_admitted(events, change):
    from app.services.governance_fact_contracts import WriterError

    writer, _, action, _, _, _ = events
    if change == 'missing_announcement': action['wire_record']['announcement_data_version'] = 'f' * 64
    elif change == 'wrong_notice': action['wire_record']['announcement_id'] = 'OTHER'
    elif change == 'wrong_publication': action['time']['published_at'] = '2026-06-01T16:00:00+08:00'
    elif change == 'unpriced_rights': action['wire_record'].update(event_type='rights_issue', rights_ratio='0.2')
    elif change == 'negative_cash': action['wire_record']['cash_dividend'] = '-1'
    elif change == 'bad_split': action['wire_record'].update(event_type='split', split_ratio='2', stock_ratio='0.5')
    elif change == 'bad_interval': action['time']['valid_to'] = None
    elif change == 'unknown_type': action['wire_record']['event_type'] = 'mystery'
    elif change == 'unnamespaced_event_id': action['key']['authority_event_id'] = 'A001:1'
    with pytest.raises(WriterError): writer.write(action, approval(writer, action))
