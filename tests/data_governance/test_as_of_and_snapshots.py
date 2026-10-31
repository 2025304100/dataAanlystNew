"""M08.1: published-snapshot as-of reads for every dataset selector.

Every snapshot here is synthetic but hash-real: members, provenance and the
published state are built from the actual fact rows, so the reader exercises the
same manifest validation an M08.2 build will have to satisfy. G03—G07/G19/G20
facts are produced by the single FactWriter, never inserted as loose payloads.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, event, insert, select, text, update

from app.db import governance_schema as s
from app.services.data_contract_profiles import FINANCIAL
from app.services.governance_data_access import DataAccessError, DataAccessService
from app.services.governance_fact_contracts import canonical, digest, hash_object
from app.services.governance_snapshot_manifest import acceptance_hash, dataset_entry, frozen_acceptance, \
    manifest_document, manifest_hash, snapshot_identity
from tests.test_governance_master_data import annual_page, code, member, publish
from tests.test_governance_schema import candidate
from tests.test_governance_writer import Clock, approval, capture_request

pytestmark = pytest.mark.whitebox

# Deterministic "today" for the wall-clock guard: after every synthetic as_of in this file.
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- helpers
def instruments(ids):
    return {'kind': 'instrument', 'instrument_ids': list(ids)}


def read_request(dataset, *, selector, start, end, as_of, mode, snapshot, profile, required,
                 price_basis='none', base_date=None):
    return {'dataset': dataset, 'selector': selector, 'event_range': {'start': start, 'end_exclusive': end},
            'as_of': as_of, 'knowledge_mode': mode, 'snapshot_id': snapshot, 'quality_profile': profile,
            'required_fields': required, 'price_basis': price_basis, 'base_date': base_date}


def sessions(master, calendar_version, exchange, hour='15:00:00'):
    document = master.calendar_snapshot(calendar_version)
    start = date.fromisoformat(document['start'])
    end = date.fromisoformat(document['end_exclusive'])
    return {value.isoformat(): value.isoformat() + 'T' + hour + '+08:00'
            for value in master.trading_days(calendar_version, exchange, start, end)}


def field(name, unit, wire=None):
    return {'wire_field': wire or name, 'native_unit': unit, 'normalized_unit': unit, 'scale': '1', 'offset': '0'}


def notice_bytes(announcement_id, event_day):
    return ('SYNTHETIC notice ' + announcement_id + ' ' + event_day).encode()


def all_profiles():
    """Every registered profile bound to its own dataset in a synthetic snapshot."""
    from app.services.data_contract_profiles import PROFILES
    result = {}
    for profile in PROFILES:
        result.setdefault(profile.profile_id.split('/', 1)[0], {})[profile.profile_id] = profile.version
    return result


class Lab:
    """Isolated candidate world: one instrument, one calendar, one writer."""

    def __init__(self, engine, master, clock, identity, instrument_version, calendar_version, contracts):
        from app.services.governance_writer import FactWriter
        self.engine = engine
        self.master = master
        self.clock = clock
        self.identity = identity
        self.instrument_version = instrument_version
        self.calendar_version = calendar_version
        self.contracts = contracts
        self.writer = FactWriter(engine, contracts=list(contracts.values()), clock=clock)
        self.service = DataAccessService(engine, clock=lambda: NOW)
        self._closes = sessions(master, calendar_version, 'SSE')

    def at(self, value):
        self.clock.now = value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _write(self, request, *, submit=True, event='T01', revision=0):
        self.last_request = request
        if not submit:
            return self.writer.preview(request)['data_version']
        return self.writer.write(request, approval(self.writer, request, revision, event))['data_version']

    def daily(self, trade_date, *, close='10', published_at='2026-06-01T16:00:00+08:00', submit=True,
              acceptance_event='T01', revision=0):
        raw = ('SYNTHETIC raw quote bytes ' + trade_date + ' ' + close).encode()
        event_day = date.fromisoformat(trade_date)
        request = {
            'dataset': 'daily_bars', 'key': {'instrument_id': self.identity, 'trade_date': trade_date},
            'contract_version': self.contracts['daily'].version, 'profile': 'daily_bars/core',
            'instrument_version': self.instrument_version, 'calendar_version': self.calendar_version,
            'wire_record': {'开盘': '10', '最高': '10.2', '最低': '9.8', '收盘': close, '成交量': '1',
                            '成交额': '1000', '换手率': '0.1'},
            'raw_response': raw, 'source_uri': 'fixture://m081/daily',
            'time': {'event_date': trade_date, 'publication_precision': 'exact', 'published_at': published_at,
                     'publication_date': None, 'valid_from': trade_date,
                     'valid_to': (event_day + timedelta(days=1)).isoformat(), 'publication_evidence': raw},
            'revision': {'kind': 'original', 'token': None, 'order': None, 'order_evidence': None,
                         'supersedes_version': None},
            'flags': {'is_suspended': False, 'synthetic_bar': False},
            'gate_results': {'P01': 'pass', 'P02': 'pass'}, 'field_sources': {}, 'batch_id': 'm081-daily-' + trade_date}
        capture_request(request)
        return self._write(request, submit=submit, event=acceptance_event, revision=revision)

    def financial(self, period, profit, *, report_type='q1', published_at='2026-04-20T10:00:00+08:00',
                  precision='exact', revision_order=None, supersedes=None, submit=True,
                  acceptance_event='T01', revision=0):
        raw = ('SYNTHETIC profit bytes ' + period + ' ' + profit + ' ' + str(published_at)).encode()
        event_day = date.fromisoformat(period)
        contract = self.contracts['financial']
        request = {
            'dataset': 'financial',
            'key': {'instrument_id': self.identity, 'report_period': period, 'report_type': report_type,
                    'definition_id': contract.document['definition_id']},
            'contract_version': contract.version, 'profile': 'financial/net_profit',
            'instrument_version': self.instrument_version, 'calendar_version': None,
            'wire_record': {'profit': profit}, 'raw_response': raw, 'source_uri': 'fixture://m081/financial',
            'time': {'event_date': period, 'publication_precision': precision,
                     'published_at': published_at if precision == 'exact' else None,
                     'publication_date': None, 'valid_from': period,
                     'valid_to': (event_day + timedelta(days=1)).isoformat(),
                     'publication_evidence': raw if precision == 'exact' else None},
            'revision': {'kind': 'original' if supersedes is None else 'correction',
                         'token': None if revision_order is None else 'rev-' + str(revision_order),
                         'order': revision_order,
                         'order_evidence': None if revision_order is None else raw,
                         'supersedes_version': supersedes},
            'flags': {}, 'gate_results': {}, 'field_sources': {},
            'batch_id': 'm081-financial-' + period + '-' + profit
        }
        return self._write(request, submit=submit, event=acceptance_event, revision=revision)

    def financial_unknown(self, period, profit, **kwargs):
        return self.financial(period, profit, published_at=None, precision='unknown', **kwargs)

    def announcement(self, announcement_id, *, event_day, published_at=None, publication_date=None,
                     submit=True, acceptance_event='T01', revision=0, supersedes=None):
        raw = notice_bytes(announcement_id, event_day)
        contract = self.contracts['announcement']
        precision = 'exact' if published_at else 'date'
        payload_time = None
        if published_at:
            payload_time = datetime.fromisoformat(published_at.replace('Z', '+00:00')).astimezone(
                timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        request = {
            'dataset': 'announcement', 'key': {'authority': 'SSE', 'announcement_id': announcement_id},
            'contract_version': contract.version, 'profile': 'announcement/core',
            'instrument_version': None, 'calendar_version': None,
            'wire_record': {'authority': 'SSE', 'announcement_id': announcement_id, 'content_hash': digest(raw),
                            'published_at': payload_time, 'publication_date': publication_date},
            'raw_response': raw, 'source_uri': 'https://www.sse.com.cn/disclosure/' + announcement_id,
            'time': {'event_date': event_day, 'publication_precision': precision, 'published_at': published_at,
                     'publication_date': publication_date, 'valid_from': event_day,
                     'valid_to': (date.fromisoformat(event_day) + timedelta(days=1)).isoformat(),
                     'publication_evidence': raw},
            'revision': {'kind': 'original' if supersedes is None else 'withdrawal',
                         'token': None, 'order': None, 'order_evidence': None, 'supersedes_version': supersedes},
            'flags': {}, 'gate_results': {}, 'field_sources': {}, 'batch_id': 'm081-notice-' + announcement_id
        }
        return self._write(request, submit=submit, event=acceptance_event, revision=revision)

    def share_capital(self, effective_date, *, notice_version, announcement_id, notice_raw,
                      published_at, total='1000000', submit=True):
        contract = self.contracts['share_capital']
        request = {
            'dataset': 'share_capital',
            'key': {'instrument_id': self.identity, 'effective_date': effective_date, 'share_class': 'A'},
            'contract_version': contract.version, 'profile': 'share_capital/core',
            'instrument_version': self.instrument_version, 'calendar_version': None,
            'wire_record': {'share_class': 'A', 'share_definition': 'cn-issued-tradable-freefloat-v1',
                            'total_shares': total, 'float_shares': str(int(total) // 2),
                            'free_float_shares': str(int(total) // 4), 'announcement_id': announcement_id,
                            'announcement_data_version': notice_version, 'event_type': 'issuance',
                            'change_reason': 'm081 share issuance'},
            'raw_response': notice_raw, 'source_uri': 'https://www.sse.com.cn/disclosure/' + announcement_id,
            'time': {'event_date': effective_date, 'publication_precision': 'exact', 'published_at': published_at,
                     'publication_date': None, 'valid_from': effective_date, 'valid_to': None,
                     'publication_evidence': notice_raw},
            'revision': {'kind': 'original', 'token': None, 'order': None, 'order_evidence': None,
                         'supersedes_version': None},
            'flags': {}, 'gate_results': {}, 'field_sources': {}, 'batch_id': 'm081-capital-' + effective_date
        }
        return self._write(request, submit=submit)

    def withdrawal(self, version, *, published_at, accept=True, retraction_reason='SYNTHETIC official retraction',
                   report_period='2026-03-31', report_type='q1'):
        contract = self.contracts['financial']
        raw = ('SYNTHETIC retraction bytes ' + version + ' ' + published_at).encode()
        request = {
            'dataset': 'financial',
            'key': {'instrument_id': self.identity, 'report_period': report_period, 'report_type': report_type,
                    'definition_id': contract.document['definition_id']},
            'contract_version': contract.version, 'profile': 'financial/net_profit',
            'instrument_version': self.instrument_version, 'calendar_version': None,
            'wire_record': {'retraction_reason': retraction_reason}, 'raw_response': raw,
            'source_uri': 'fixture://m081/retraction',
            'time': {'event_date': report_period, 'publication_precision': 'exact', 'published_at': published_at,
                     'publication_date': None, 'valid_from': report_period,
                     'valid_to': (date.fromisoformat(report_period) + timedelta(days=1)).isoformat(),
                     'publication_evidence': raw},
            'revision': {'kind': 'withdrawal', 'token': None, 'order': None, 'order_evidence': None,
                         'supersedes_version': version},
            'flags': {}, 'gate_results': {}, 'field_sources': {}, 'batch_id': 'm081-retraction-' + version[:8]
        }
        return self._write(request, submit=accept, event='T12', revision=1)

    def accept_last(self, *, event='T12', revision=1):
        """Second phase for a staged fact: approve exactly the last prepared request."""
        return self.writer.write(deepcopy(self.last_request),
                                 approval(self.writer, self.last_request, revision, event))['data_version']

    # ---------------------------------------------------------------- snapshot
    def dependencies(self, *, adjustment_sets=(), instrument_version=None):
        closes = {'SSE': self._closes, 'INDEX': self._closes}
        return {'instrument_version': instrument_version or self.instrument_version,
                'calendar_versions': [self.calendar_version],
                'session_closes': {'calendar_version': self.calendar_version, 'closes': closes,
                                   'evidence_sha256': digest(canonical(closes).encode('utf-8'))},
                'adjustment_sets': sorted(adjustment_sets)}

    @staticmethod
    def _bindings(db, name, versions):
        """Complete R3 bindings: the unit contract and definition identity of the members."""
        facts = db.execute(select(s.fact_versions.c.data_version, s.fact_versions.c.contract_version,
                                  s.fact_versions.c.definition_id)
                           .where(s.fact_versions.c.data_version.in_(versions))).mappings().all()
        profiles = {row['data_version']: row['quality_profile'] for row in db.execute(
            select(s.fact_current.c.data_version, s.fact_current.c.quality_profile)
            .where(s.fact_current.c.data_version.in_(versions))).mappings()}
        contracts, definitions = {}, set()
        for row in facts:
            contracts[row['contract_version']] = profiles.get(row['data_version']) or name + '/core'
            definitions.add(row['definition_id'])
        # No per-fact source policy identity exists in R2/R3 yet: bound as empty, not invented.
        return {'contract_versions': contracts, 'definition_versions': sorted(definitions),
                'source_policy_versions': []}

    def publish(self, members, *, profiles=None, dependencies=None, state='published',
                created_at='2026-08-01T00:00:00.000000Z', published_at=datetime(2026, 8, 1, 1),
                instrument_links=None, mutate=None):
        profiles = profiles or {}
        entries = {}
        with self.engine.begin() as db:
            for name, versions in members.items():
                versions = sorted(set(versions))
                records = db.execute(select(s.acceptance_history)
                                     .where(s.acceptance_history.c.data_version.in_(versions))).mappings().all() if versions else []
                accepted = acceptance_hash(name, frozen_acceptance(records, created_at))
                bindings = self._bindings(db, name, versions) if versions else {
                    'contract_versions': {}, 'definition_versions': [], 'source_policy_versions': []}
                entries[name] = dataset_entry(dataset=name, members=versions,
                                              acceptance_history_hash=accepted, created_at=created_at,
                                              contract_versions=bindings['contract_versions'],
                                              definition_versions=bindings['definition_versions'],
                                              source_policy_versions=bindings['source_policy_versions'],
                                              quality_profile_versions=profiles.get(name, {}),
                                              instrument_links=instrument_links)
                db.execute(insert(s.dataset_versions).values(
                    dataset_version=entries[name]['dataset_version'], dataset=name,
                    member_hash=entries[name]['member_hash'], acceptance_history_hash=accepted,
                    contract_versions=bindings['contract_versions'],
                    source_policy_versions=bindings['source_policy_versions'],
                    created_at=datetime.fromisoformat(created_at.replace('Z', '+00:00')).replace(tzinfo=None)))
                if versions:
                    db.execute(insert(s.dataset_members),
                               [{'dataset_version': entries[name]['dataset_version'], 'data_version': value}
                                for value in versions])
            document = manifest_document(created_at=created_at, datasets=entries,
                                         dependencies=dependencies or self.dependencies())
            if mutate is not None:
                document = mutate(deepcopy(document))
            snapshot_id = snapshot_identity(document)
            db.execute(insert(s.snapshot_manifest).values(
                snapshot_id=snapshot_id, manifest_json=canonical(document), manifest_hash=manifest_hash(document),
                state=state, created_at=datetime.fromisoformat(created_at.replace('Z', '+00:00')).replace(tzinfo=None),
                published_at=published_at if state == 'published' else None))
            db.execute(insert(s.snapshot_datasets),
                       [{'snapshot_id': snapshot_id, 'dataset': name, 'dataset_version': entry['dataset_version']}
                        for name, entry in entries.items()])
        # A read happens after publication: move the test clock past it without
        # ever moving it backwards for later fact writes.
        published = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
        if self.clock.now < published + timedelta(hours=1):
            self.clock.now = published + timedelta(hours=1)
        return snapshot_id

    def read(self, request):
        return self.service.read_as_of(request)


def profile_versions(lab, name):
    """Frozen profile versions bound into a snapshot entry."""
    from app.services.data_contract_profiles import get_profile
    contract = lab.contracts[name] if name in lab.contracts else None
    if name == 'financial':
        return {'financial/net_profit': get_profile('financial/net_profit').version,
                'financial/core': get_profile('financial/core').version}
    profile_id = {'announcement': 'announcement/core', 'share_capital': 'share_capital/core',
                  'daily': 'daily_bars/core'}[name]
    return {profile_id: get_profile(profile_id).version}


@pytest.fixture
def lab(candidate, annual_page):
    from app.db.governance_migration import migrate_candidate
    from app.services.governance_master_data import MasterDataService
    from app.services.governance_writer import WriterContract
    from app.services.trade_calendar_sync import SOURCE_URL

    migrate_candidate(candidate)
    master = MasterDataService(candidate)
    identity = master.register_identity('authority:m081-security', asset_type='stock', exchange='SSE')
    instrument_version = publish(master, [member(identity)], [code(identity, '600001')], [])
    calendar_version = master.publish_calendar(exchange='SSE', source_uri=SOURCE_URL, evidence=annual_page)
    clock = Clock()
    contracts = {
        'daily': WriterContract.daily_adapter(provider='eastmoney', endpoint='stock_zh_a_hist', asset_type='stock',
                                             instrument_family='sh_main',
                                             definition={'name': 'raw-price-CNY/share', 'version': 1},
                                             evidence=b'ISOLATED M08.1 raw/unit contract',
                                             publication_timezone='Asia/Shanghai'),
        'financial': WriterContract.profile(
            dataset='financial', profile='financial/net_profit', provider='baostock', endpoint='profit_data',
            asset_type='stock', instrument_family='sh_main',
            definition={'scope': 'consolidated', 'profit': 'parent', 'period': 'cumulative'},
            fields={'net_profit_parent': field('net_profit_parent', 'CNY', 'profit')},
            evidence=b'ISOLATED M08.1 financial unit proof', publication_timezone='Asia/Shanghai',
            revision_order='integer'),
        'announcement': WriterContract.profile(
            dataset='announcement', profile='announcement/core', provider='SSE', endpoint='disclosure',
            asset_type='stock', instrument_family='sh_main', definition={'schema': 'announcement-official-v1'},
            fields={name: field(name, unit) for name, unit in {
                'authority': 'text', 'announcement_id': 'text', 'content_hash': 'hash',
                'published_at': 'time', 'publication_date': 'date'}.items()},
            evidence=b'ISOLATED M08.1 SSE notice mapping', publication_timezone='Asia/Shanghai'),
        'share_capital': WriterContract.profile(
            dataset='share_capital', profile='share_capital/core', provider='SSE', endpoint='disclosure',
            asset_type='stock', instrument_family='sh_main', definition={'schema': 'share-capital-official-v1'},
            fields={name: field(name, unit) for name, unit in {
                'share_class': 'text', 'share_definition': 'text', 'total_shares': 'share',
                'float_shares': 'share', 'free_float_shares': 'share', 'announcement_id': 'text',
                'announcement_data_version': 'hash', 'event_type': 'text',
                'change_reason': 'text'}.items()},
            evidence=b'ISOLATED M08.1 share capital mapping', publication_timezone='Asia/Shanghai',
            revision_order='integer'),
    }
    world = Lab(candidate, master, clock, identity, instrument_version, calendar_version, contracts)
    # 14.12.3: an index id is a registered namespace, not any non-empty string.
    from app.services.governance_index import IndexService, install_index_schema
    install_index_schema(candidate)
    IndexService(world.writer).register_index(
        {'index_id': 'CSI:000300', 'authority': 'CSI', 'quote_symbol': '000300', 'quote_market': 'sh',
         'eastmoney_secid': '1.000300', 'calendar_exchange': 'SSE', 'series_kind': 'price',
         'valid_from': '2020-01-01', 'valid_to': None, 'expected_members': 1},
        source_evidence=b'ISOLATED M08.1 namespace authority record',
        review_evidence=b'ISOLATED M08.1 explicit namespace review')
    return world


# --------------------------------------------------------------------------- fixtures
DAILY_REQUIRED = ['raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume_shares', 'is_suspended', 'synthetic_bar']
FIN_REQUIRED = ['net_profit_parent']
FIN_CORE_REQUIRED = list(FINANCIAL)
SHARE_REQUIRED = ['share_class', 'share_definition', 'total_shares', 'float_shares', 'announcement_id',
                  'announcement_data_version', 'event_type', 'change_reason']


def fresh(lab):
    """A reader without snapshot/fact cache, for post-mutation assertions."""
    return DataAccessService(lab.engine, clock=lambda: NOW)


def fin(lab, snapshot, as_of, mode='public_known', *, profile='financial/net_profit',
        required=None, **overrides):
    request = read_request('financial', selector=instruments([lab.identity]), start='2026-01-01',
                           end='2027-01-01', as_of=as_of, mode=mode, snapshot=snapshot,
                           profile=profile, required=FIN_REQUIRED if required is None else required)
    request.update(overrides)
    return request


def reasons(result):
    return sorted({item['reason'] for item in result['missing']})


def basic(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(),
                           created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    return snapshot, version


# ------------------------------------------------------------------- G03 share capital
def test_g03_share_capital_is_unreadable_before_its_own_public_and_system_times(lab):
    lab.at(datetime(2026, 4, 10, 2, tzinfo=timezone.utc))
    notice = lab.announcement('A-G03', event_day='2026-04-10', published_at='2026-04-10T10:00:00+08:00')
    lab.at(datetime(2026, 4, 11, 1, tzinfo=timezone.utc))
    capital = lab.share_capital('2026-04-01', notice_version=notice, announcement_id='A-G03',
                                notice_raw=notice_bytes('A-G03', '2026-04-10'),
                                published_at='2026-04-10T10:00:00+08:00')
    snapshot = lab.publish({'share_capital': [capital], 'announcement': [notice]}, profiles=all_profiles(),
                           created_at='2026-04-12T00:00:00.000000Z', published_at=datetime(2026, 4, 12, 1),
                           instrument_links={str(lab.identity): ['A-G03']})

    def read(as_of, mode):
        return fresh(lab).read_as_of(read_request(
            'share_capital', selector=instruments([lab.identity]), start='2026-01-01', end='2027-01-01',
            as_of=as_of, mode=mode, snapshot=snapshot, profile='share_capital/core', required=SHARE_REQUIRED))

    before_public = read('2026-04-05T10:00:00+08:00', 'public_known')
    assert before_public['rows'] == [] and reasons(before_public) == ['NOT_KNOWN']
    before_system = read('2026-04-05T10:00:00+08:00', 'system_known')
    assert before_system['rows'] == [] and reasons(before_system) == ['NOT_INGESTED']

    public_day = read('2026-04-10T12:00:00+08:00', 'public_known')
    assert len(public_day['rows']) == 1 and public_day['rows'][0]['total_shares'] == '1000000.00000000'
    assert public_day['reconstruction'] is True and public_day['missing'] == []
    system_day = read('2026-04-10T12:00:00+08:00', 'system_known')
    assert system_day['rows'] == [] and reasons(system_day) == ['NOT_INGESTED']

    after_system = read('2026-04-11T09:30:00+08:00', 'system_known')
    assert len(after_system['rows']) == 1 and after_system['rows'][0]['available_at'] == '2026-04-11T09:00:00+08:00'


# ------------------------------------------------------------------- G04 correction
def test_g04_correction_visibility_differs_between_public_and_system_knowledge(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    first = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00', revision_order=1)
    lab.at(datetime(2026, 6, 2, 1, tzinfo=timezone.utc))
    second = lab.financial('2026-03-31', '80', published_at='2026-06-01T10:00:00+08:00',
                           revision_order=2, supersedes=first, acceptance_event='T09', revision=1)
    snapshot = lab.publish({'financial': [first, second]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1))

    april_public = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    april_system = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00', 'system_known'))
    assert april_public['rows'][0]['net_profit_parent'] == '100.00000000'
    assert april_system['rows'][0]['net_profit_parent'] == '100.00000000'
    assert april_public['rows'][0]['quality_status'] == 'partial'
    assert april_public['rows'][0]['request_quality_status'] == 'complete'

    june_public = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-01T12:00:00+08:00'))
    june_system = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-01T12:00:00+08:00', 'system_known'))
    assert june_public['rows'][0]['net_profit_parent'] == '80.00000000'
    assert june_system['rows'][0]['net_profit_parent'] == '100.00000000'

    settled = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-02T10:00:00+08:00'))
    settled_system = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-02T10:00:00+08:00', 'system_known'))
    assert settled['rows'][0]['net_profit_parent'] == '80.00000000'
    assert settled_system['rows'][0]['net_profit_parent'] == '80.00000000'


# ------------------------------------------------------------------- G05 date only
def test_g05_date_only_notice_is_public_at_next_shanghai_midnight(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    notice = lab.announcement('A-G05', event_day='2026-04-20', publication_date='2026-04-20')
    snapshot = lab.publish({'announcement': [notice]}, profiles=all_profiles(),
                           created_at='2026-04-22T00:00:00.000000Z', published_at=datetime(2026, 4, 22, 1),
                           instrument_links={str(lab.identity): ['A-G05']})

    def read(as_of):
        return fresh(lab).read_as_of(read_request(
            'announcement', selector={'kind': 'announcement', 'authority': 'SSE', 'announcement_ids': ['A-G05']},
            start='2026-04-01', end='2026-05-01', as_of=as_of, mode='public_known', snapshot=snapshot,
            profile='announcement/core', required=['authority', 'announcement_id', 'content_hash']))

    same_day = read('2026-04-20T15:00:00+08:00')
    assert same_day['rows'] == [] and reasons(same_day) == ['NOT_KNOWN']
    next_day = read('2026-04-21T09:00:00+08:00')
    row = next_day['rows'][0]
    assert row['public_available_at'] == '2026-04-21T00:00:00+08:00'
    assert row['publication_date'] == '2026-04-20' and row['publication_precision'] == 'date'
    assert row['published_at'] is None


# --------------------------------------------------------------- G06 unknown publish
def test_g06_unknown_publication_stays_not_known_for_public_history(lab):
    lab.at(datetime(2026, 7, 1, 2, tzinfo=timezone.utc))
    version = lab.financial_unknown('2026-03-31', '100')
    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(),
                           created_at='2026-07-02T00:00:00.000000Z', published_at=datetime(2026, 7, 2, 1))

    april_public = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    april_system = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00', 'system_known'))
    assert april_public['rows'] == [] and reasons(april_public) == ['NOT_KNOWN']
    assert april_system['rows'] == [] and reasons(april_system) == ['NOT_INGESTED']

    collected_system = fresh(lab).read_as_of(fin(lab, snapshot, '2026-07-02T09:00:00+08:00', 'system_known'))
    assert collected_system['rows'][0]['net_profit_parent'] == '100.00000000'
    collected_public = fresh(lab).read_as_of(fin(lab, snapshot, '2026-07-02T09:00:00+08:00'))
    assert collected_public['rows'] == [] and reasons(collected_public) == ['NOT_KNOWN']


# --------------------------------------------------------------------- G07 withdrawal
def test_g07_withdrawal_never_falls_back_and_takes_effect_at_each_visibility(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    original = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    lab.at(datetime(2026, 6, 3, 2, tzinfo=timezone.utc))
    lab.withdrawal(original, published_at='2026-06-03T10:00:00+08:00', accept=False)
    lab.at(datetime(2026, 6, 4, 1, tzinfo=timezone.utc))
    retraction = lab.accept_last(event='T12', revision=1)
    snapshot = lab.publish({'financial': [original, retraction]}, profiles=all_profiles(),
                           created_at='2026-06-05T00:00:00.000000Z', published_at=datetime(2026, 6, 5, 1))

    value_before = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-02T15:00:00+08:00'))
    assert value_before['rows'][0]['event_date'] == '2026-03-31'
    public_after = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-03T12:00:00+08:00'))
    assert public_after['rows'] == [] and reasons(public_after) == ['RETRACTED']
    system_between = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-03T12:00:00+08:00', 'system_known'))
    assert system_between['rows'][0]['net_profit_parent'] == '100.00000000'
    system_after = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-04T10:00:00+08:00', 'system_known'))
    assert system_after['rows'] == [] and reasons(system_after) == ['RETRACTED']


# ----------------------------------------------------------------- G19 half-open range
def test_g19_exact_interval_start_returns_only_the_new_version(lab):
    lab.at(datetime(2026, 6, 2, 10, tzinfo=timezone.utc))
    first = lab.financial('2026-03-31', '100', published_at='2026-06-02T17:00:00+08:00', revision_order=1)
    lab.at(datetime(2026, 6, 2, 11, tzinfo=timezone.utc))
    second = lab.financial('2026-03-31', '200', published_at='2026-06-02T18:00:00+08:00',
                           revision_order=2, supersedes=first, acceptance_event='T09', revision=1)
    lab.at(datetime(2026, 6, 2, 12, tzinfo=timezone.utc))
    snapshot = lab.publish({'financial': [first, second]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1))

    before = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-02T10:59:59Z', 'system_known'))
    boundary = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-02T11:00:00Z', 'system_known'))
    after = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-02T11:00:01Z', 'system_known'))
    assert [row['net_profit_parent'] for row in before['rows']] == ['100.00000000']
    assert [row['net_profit_parent'] for row in boundary['rows']] == ['200.00000000']
    assert [row['net_profit_parent'] for row in after['rows']] == ['200.00000000']
    assert boundary['rows'][0]['data_version'] == second


# ------------------------------------------------------- G20 revised old report
def test_g20_old_period_correction_does_not_override_the_latest_period(lab):
    lab.at(datetime(2026, 7, 2, 2, tzinfo=timezone.utc))
    q1 = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00', revision_order=1)
    half = lab.financial('2026-06-30', '120', report_type='half_year', published_at='2026-07-02T10:00:00+08:00')
    lab.at(datetime(2026, 7, 5, 2, tzinfo=timezone.utc))
    corrected = lab.financial('2026-03-31', '90', published_at='2026-07-05T10:00:00+08:00',
                              revision_order=2, supersedes=q1, acceptance_event='T09', revision=1)
    snapshot = lab.publish({'financial': [q1, half, corrected]}, profiles=all_profiles(),
                           created_at='2026-07-10T00:00:00.000000Z', published_at=datetime(2026, 7, 10, 6))

    result = fresh(lab).read_as_of(fin(lab, snapshot, '2026-07-10T12:00:00+08:00', 'system_known'))
    rows = {row['event_date']: row for row in result['rows']}
    assert set(rows) == {'2026-03-31', '2026-06-30'}
    assert rows['2026-03-31']['net_profit_parent'] == '90.00000000'
    assert rows['2026-03-31']['data_version'] == corrected
    assert rows['2026-06-30']['net_profit_parent'] == '120.00000000'
    assert max(rows) == '2026-06-30'


# ------------------------------------------------------- every dataset selector
def test_every_dataset_selector_is_served_or_reported_missing(lab):
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    notice = lab.announcement('A-12', event_day='2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    capital = lab.share_capital('2026-06-01', notice_version=notice, announcement_id='A-12',
                                notice_raw=notice_bytes('A-12', '2026-06-01'),
                                published_at='2026-06-01T16:00:00+08:00')
    daily = lab.daily('2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    financial = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    members = {'daily_bars': [daily], 'financial': [financial], 'announcement': [notice],
               'share_capital': [capital],
               'valuation': [], 'capital_flow': [], 'lhb': [], 'hot_rank': [], 'etf': [],
               'index': [], 'macro': [], 'corporate_action': []}
    snapshot = lab.publish(members, profiles=all_profiles(), created_at='2026-06-03T00:00:00.000000Z',
                           published_at=datetime(2026, 6, 3, 1),
                           instrument_links={str(lab.identity): ['A-12']})
    common = {'as_of': '2026-06-02T09:00:00+08:00', 'mode': 'system_known', 'snapshot': snapshot}
    cases = [
        ('daily_bars', instruments([lab.identity]), 'daily_bars/core', DAILY_REQUIRED, True),
        ('valuation', instruments([lab.identity]), 'valuation/core', ['pe_ttm_x', 'pb_mrq_x', 'definition_id'], False),
        ('financial', instruments([lab.identity]), 'financial/net_profit', FIN_REQUIRED, True),
        ('capital_flow', instruments([lab.identity]), 'capital_flow/core',
         ['main', 'super_large', 'large', 'medium', 'small', 'flow_definition'], False),
        ('lhb', instruments([lab.identity]), 'lhb/core',
         ['institution_buy', 'institution_sell', 'institution_net', 'reason_scope', 'dedup_rule_id'], False),
        ('hot_rank', instruments([lab.identity]), 'hot_rank/core',
         ['rank', 'ranking_method', 'observation_at'], False),
        ('etf', instruments([lab.identity]), 'etf/nav', ['nav_official', 'valuation_date', 'currency'], False),
        ('index', {'kind': 'index', 'index_ids': ['CSI:000300']}, 'index/core',
         ['index_open', 'index_high', 'index_low', 'index_close', 'index_id'], False),
        ('macro', {'kind': 'macro', 'indicator_keys': ['cn_cpi_yoy']}, 'macro/core',
         ['indicator_key', 'period', 'frequency', 'value', 'unit'], False),
        ('announcement', {'kind': 'announcement', 'authority': 'SSE', 'announcement_ids': ['A-12']},
         'announcement/core', ['authority', 'announcement_id', 'content_hash'], True),
        ('share_capital', instruments([lab.identity]), 'share_capital/core', SHARE_REQUIRED, True),
        ('corporate_action', instruments([lab.identity]), 'corporate_action/core',
         ['event_type', 'cash_dividend', 'stock_ratio', 'rights_ratio', 'announcement_id',
          'announcement_data_version'], False),
    ]
    ranges = {'daily_bars': ('2026-06-01', '2026-06-02')}
    for dataset, selector, profile, required, has_rows in cases:
        start, end = ranges.get(dataset, ('2026-01-01', '2026-07-01'))
        request = read_request(dataset, selector=selector, profile=profile, required=required,
                               start=start, end=end, **common)
        result = fresh(lab).read_as_of(request)
        if has_rows:
            assert result['rows'], dataset
        else:
            assert result['rows'] == [], dataset
            assert reasons(result) == ['NOT_INGESTED'], (dataset, result['missing'])


def test_announcement_instrument_selector_uses_only_explicit_snapshot_links(lab):
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    notice = lab.announcement('A-13', event_day='2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    other = lab.master.register_identity('authority:m081-security-b', asset_type='stock', exchange='SSE')
    snapshot = lab.publish({'announcement': [notice]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1),
                           instrument_links={str(lab.identity): ['A-13']})

    def read(instrument_id):
        return fresh(lab).read_as_of(read_request(
            'announcement', selector=instruments([instrument_id]), start='2026-06-01', end='2026-06-02',
            as_of='2026-06-02T09:00:00+08:00', mode='system_known', snapshot=snapshot,
            profile='announcement/core', required=['authority', 'announcement_id', 'content_hash']))

    linked = read(lab.identity)
    assert [row['announcement_id'] for row in linked['rows']] == ['A-13'], linked['missing']
    unlinked = read(other)
    assert unlinked['rows'] == [] and unlinked['missing'][0]['detail'] == 'NO_ANNOUNCEMENT_LINK'


def test_literal_spec_manifest_without_the_extension_key_still_serves_links(lab):
    """14.12.3 / 8.1: a governance-1.4 manifest carries no extra keys.

    The frozen key set is exact, so the reader must also work when the optional
    announcement extension is absent: the instrument relation is then rebuilt from
    the member facts that name the announcement (never from a code guess).
    """
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    notice = lab.announcement('A-14', event_day='2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    capital = lab.share_capital('2026-06-01', notice_version=notice, announcement_id='A-14',
                                notice_raw=notice_bytes('A-14', '2026-06-01'),
                                published_at='2026-06-01T16:00:00+08:00')

    def literal(document):
        document['datasets']['announcement'].pop('instrument_links')
        return document

    snapshot = lab.publish({'announcement': [notice], 'share_capital': [capital]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1),
                           instrument_links={str(lab.identity): ['A-14']}, mutate=literal)
    result = fresh(lab).read_as_of(read_request(
        'announcement', selector=instruments([lab.identity]), start='2026-06-01', end='2026-06-02',
        as_of='2026-06-02T09:00:00+08:00', mode='system_known', snapshot=snapshot,
        profile='announcement/core', required=['authority', 'announcement_id', 'content_hash']))
    assert [row['announcement_id'] for row in result['rows']] == ['A-14'], result['missing']
    assert result['missing'] == []


def test_unregistered_instrument_is_rejected_before_an_unknown_snapshot(lab):
    """14.12.3: an unknown ID is INVALID_ARGUMENT/400 even if the snapshot id is bogus.

    Request-scope rejections must not be masked by a snapshot error, otherwise callers
    cannot tell "you asked for something unregistered" from "that snapshot is missing".
    """
    request = read_request('financial', selector=instruments([999999]), start='2026-01-01',
                           end='2027-01-01', as_of='2026-04-30T15:00:00+08:00', mode='public_known',
                           snapshot='f' * 64, profile='financial/net_profit', required=FIN_REQUIRED)
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(request)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.status == 400
    assert exc.value.http_status == 400 and exc.value.detail == 'INSTRUMENT_NOT_REGISTERED'


def test_calendar_dataset_missing_reason_follows_the_knowledge_mode(lab):
    """4.4: one governance fact must not carry two reasons across dataset branches."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    snapshot = lab.publish({'financial': [version], 'daily_bars': []}, profiles=all_profiles(),
                           created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    outsider = lab.master.register_identity('authority:m081-security-d', asset_type='stock', exchange='SSE')

    def read(mode):
        return fresh(lab).read_as_of(read_request(
            'daily_bars', selector=instruments([outsider]), start='2026-06-01', end='2026-06-02',
            as_of='2026-04-30T15:00:00+08:00', mode=mode, snapshot=snapshot,
            profile='daily_bars/core', required=DAILY_REQUIRED, price_basis='raw'))

    public = read('public_known')
    assert public['rows'] == []
    assert [(item['reason'], item['detail']) for item in public['missing']] == [
        ('NOT_KNOWN', 'INSTRUMENT_NOT_IN_SNAPSHOT')]
    system = read('system_known')
    assert system['rows'] == []
    assert [(item['reason'], item['detail']) for item in system['missing']] == [
        ('NOT_INGESTED', 'INSTRUMENT_NOT_IN_SNAPSHOT')]


def test_instrument_must_be_registered_but_snapshot_membership_is_reported(lab):
    snapshot, _ = basic(lab)
    outsider = lab.master.register_identity('authority:m081-security-c', asset_type='stock', exchange='SSE')

    def read(instrument_id):
        return fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00',
                                         selector=instruments([instrument_id])))

    member = read(outsider)
    assert member['rows'] == [] and member['missing'][0]['detail'] == 'INSTRUMENT_NOT_IN_SNAPSHOT'
    with pytest.raises(DataAccessError) as exc:
        read(999999)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'INSTRUMENT_NOT_REGISTERED'


def test_macro_indicator_registry_separates_unknown_from_not_ingested(lab):
    snapshot, _ = basic(lab)
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(read_request(
            'macro', selector={'kind': 'macro', 'indicator_keys': ['not_a_registered_indicator']},
            start='2026-01-01', end='2026-02-01', as_of='2026-04-30T15:00:00+08:00', mode='system_known',
            snapshot=snapshot, profile='macro/core',
            required=['indicator_key', 'period', 'frequency', 'value', 'unit']))
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'INDICATOR_UNREGISTERED'


# ------------------------------------------------------------- snapshot boundary
def dump(engine):
    names = ('fact_versions', 'fact_current', 'acceptance_history', 'version_availability', 'source_decisions',
             'snapshot_manifest', 'snapshot_datasets', 'dataset_members', 'dataset_versions')
    state = {}
    with engine.connect() as db:
        for name in names:
            rows = db.execute(select(s.metadata.tables[name])).mappings().all()
            state[name] = sorted(str(sorted(dict(row).items(), key=lambda item: item[0])) for row in rows)
    return state


def test_later_current_changes_do_not_leak_into_an_older_snapshot(lab):
    snapshot, version = basic(lab)
    first = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert first['rows'][0]['net_profit_parent'] == '100.00000000'
    lab.at(datetime(2026, 5, 2, 2, tzinfo=timezone.utc))
    lab.financial('2026-03-31', '70', published_at='2026-05-02T10:00:00+08:00', revision_order=2,
                  supersedes=version, acceptance_event='T09', revision=1)
    again = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert canonical(again) == canonical(first)
    assert again['rows'][0]['data_version'] == version


def test_unpublished_corrupt_or_unbound_snapshots_are_refused(lab):
    snapshot, version = basic(lab)
    building = lab.publish({'financial': [version]}, profiles=all_profiles(), state='building',
                           created_at='2026-04-23T00:00:00.000000Z', published_at=datetime(2026, 4, 23, 1))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, building, '2026-04-30T15:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_NOT_PUBLISHED' and exc.value.status == 409

    hashed = lab.publish({'financial': [version]}, profiles=all_profiles(),
                         created_at='2026-05-02T00:00:00.000000Z', published_at=datetime(2026, 5, 2, 1))
    with lab.engine.begin() as db:
        dataset_version = db.execute(select(s.snapshot_datasets.c.dataset_version)
                                     .where(s.snapshot_datasets.c.snapshot_id == hashed)).scalar_one()
        db.execute(update(s.dataset_versions).where(s.dataset_versions.c.dataset_version == dataset_version)
                   .values(member_hash='f' * 64))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, hashed, '2026-04-30T15:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_NOT_PUBLISHED' and 'MEMBER_HASH_MISMATCH' in exc.value.detail

    with lab.engine.begin() as db:
        db.execute(update(s.snapshot_manifest).where(s.snapshot_manifest.c.snapshot_id == snapshot)
                   .values(manifest_json='{"schema_version":"governance-1.4"}'))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_NOT_PUBLISHED' and 'MANIFEST_HASH_MISMATCH' in exc.value.detail


def test_missing_dependency_or_supersedes_parent_is_a_dependency_error(lab):
    snapshot, version = basic(lab)
    broken = lab.publish({'financial': [version]}, profiles=all_profiles(),
                         created_at='2026-04-24T00:00:00.000000Z', published_at=datetime(2026, 4, 24, 1),
                         dependencies=lab.dependencies(instrument_version='f' * 64))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, broken, '2026-04-30T15:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_DEPENDENCY_MISSING' and exc.value.status == 409
    assert exc.value.detail == 'INSTRUMENT_VERSION_UNAVAILABLE'

    lab.at(datetime(2026, 5, 2, 2, tzinfo=timezone.utc))
    child = lab.financial('2026-03-31', '70', published_at='2026-05-02T10:00:00+08:00', revision_order=2,
                          supersedes=version, acceptance_event='T09', revision=1)
    parentless = lab.publish({'financial': [child]}, profiles=all_profiles(),
                             created_at='2026-05-03T00:00:00.000000Z', published_at=datetime(2026, 5, 3, 1))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, parentless, '2026-05-02T12:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_DEPENDENCY_MISSING'
    assert exc.value.detail == 'SUPERSEDES_OUTSIDE_SNAPSHOT'


def test_unsorted_public_branch_is_ambiguous_instead_of_insertion_ordered(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    original = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    lab.at(datetime(2026, 4, 25, 1, tzinfo=timezone.utc))
    # T11 arbitration activates a different version of the same key with no ordering evidence.
    rival = lab.financial('2026-03-31', '90', published_at='2026-04-24T10:00:00+08:00',
                          acceptance_event='T11', revision=1)
    snapshot = lab.publish({'financial': [original, rival]}, profiles=all_profiles(),
                           created_at='2026-04-26T00:00:00.000000Z', published_at=datetime(2026, 4, 26, 1))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-25T12:00:00+08:00'))
    assert exc.value.code == 'VERSION_AMBIGUOUS' and exc.value.status == 409


def test_overlapping_acceptance_intervals_are_ambiguous_not_last_wins(lab):
    """Two acceptance intervals covering one instant is a contract error, not last-wins.

    The second (fault-injected) record is injected *before* publication, so the frozen
    acceptance hash covers it; a state changed after publication is a dependency failure
    instead (R01), never a silently different row.
    """
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    with lab.engine.begin() as db:
        row = db.execute(select(s.acceptance_history).where(s.acceptance_history.c.data_version == version)).mappings().one()
        db.execute(insert(s.acceptance_history).values(
            dataset=row['dataset'], logical_key=row['logical_key'], pointer_revision=99,
            data_version=row['data_version'], source_state=row['source_state'], quality_status=row['quality_status'],
            system_from=row['system_from'], system_to=None, decision_id=row['decision_id']))
    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(),
                           created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00', 'system_known'))
    assert exc.value.code == 'VERSION_AMBIGUOUS' and exc.value.detail == 'SYSTEM_INTERVAL_OVERLAP'


# -------------------------------------------------------------- profile / gates
def test_narrow_profile_reads_while_core_is_quality_blocked(lab):
    snapshot, _ = basic(lab)
    narrow = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert narrow['rows'][0]['request_quality_status'] == 'complete'
    assert narrow['rows'][0]['quality_status'] == 'partial'

    core = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00',
                                     profile='financial/core', required=FIN_CORE_REQUIRED))
    assert core['rows'] == [] and reasons(core) == ['QUALITY_BLOCKED']
    assert core['missing'][0]['detail'] == 'REQUEST_PROFILE_INCOMPLETE:financial/core'


def test_quarantined_or_conflicting_acceptance_is_not_consumable(lab):
    # R01: the state is part of the acceptance history the snapshot freezes, so it is set
    # before publication; changed provenance after publication is a dependency failure.
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    with lab.engine.begin() as db:
        db.execute(update(s.acceptance_history).where(s.acceptance_history.c.data_version == version)
                   .values(quality_status='quarantined'))
    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(),
                           created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    quarantined = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert quarantined['rows'] == [] and quarantined['missing'][0]['detail'] == 'QUALITY_QUARANTINED'

    with lab.engine.begin() as db:
        db.execute(update(s.acceptance_history).where(s.acceptance_history.c.data_version == version)
                   .values(quality_status='partial', source_state='conflict'))
    second = lab.publish({'financial': [version]}, profiles=all_profiles(),
                         created_at='2026-05-02T00:00:00.000000Z', published_at=datetime(2026, 5, 2, 1))
    conflict = fresh(lab).read_as_of(fin(lab, second, '2026-04-30T15:00:00+08:00'))
    assert conflict['rows'] == [] and reasons(conflict) == ['QUALITY_BLOCKED']
    assert conflict['missing'][0]['detail'] == 'SOURCE_STATE_NOT_CONSUMABLE:conflict'


def test_backup_temp_is_returned_as_explicitly_degraded(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    with lab.engine.begin() as db:
        db.execute(update(s.acceptance_history).where(s.acceptance_history.c.data_version == version)
                   .values(source_state='backup_temp'))
    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(),
                           created_at='2026-05-03T00:00:00.000000Z', published_at=datetime(2026, 5, 3, 1))
    degraded = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert degraded['rows'][0]['degraded'] is True
    assert degraded['rows'][0]['source_state'] == 'backup_temp'


@pytest.mark.parametrize('profile,required,detail', [
    ('financial/core', ['net_profit_parent'], 'REQUIRED_FIELDS_INCOMPLETE'),
    ('financial/net_profit', ['net_profit_parent', 'unknown_field'], 'REQUIRED_FIELDS_UNKNOWN'),
    ('financial/net_profit', [], 'REQUIRED_FIELDS_EMPTY'),
    ('financial/net_profit', ['net_profit_parent', 'net_profit_parent'], 'REQUIRED_FIELDS_DUPLICATE'),
    ('financial/not_registered', ['net_profit_parent'], 'PROFILE_UNREGISTERED'),
    ('valuation/core', ['pe_ttm_x', 'pb_mrq_x', 'definition_id'], 'PROFILE_DATASET_MISMATCH'),
])
def test_profile_and_required_field_scope_are_enforced(lab, profile, required, detail):
    snapshot, _ = basic(lab)
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00', profile=profile, required=required))
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == detail


def test_unbound_profile_in_a_snapshot_is_a_dependency_error(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    snapshot = lab.publish({'financial': [version]}, created_at='2026-05-01T00:00:00.000000Z',
                           published_at=datetime(2026, 5, 1, 1))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_DEPENDENCY_MISSING' and exc.value.detail == 'PROFILE_NOT_BOUND'


# --------------------------------------------------------------------- raw / adj
def test_raw_price_basis_returns_unaltered_raw_fields(lab):
    from app.services.governance_prices import install_price_schema  # noqa: F401
    lab.at(datetime(2026, 6, 2, 8, tzinfo=timezone.utc))
    version = lab.daily('2026-06-02', published_at='2026-06-02T16:00:00+08:00')
    snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1))
    raw = fresh(lab).read_as_of(read_request(
        'daily_bars', selector=instruments([lab.identity]), start='2026-06-01', end='2026-06-03',
        as_of='2026-06-02T17:00:00+08:00', mode='system_known', snapshot=snapshot,
        profile='daily_bars/core', required=DAILY_REQUIRED, price_basis='raw'))
    row = raw['rows'][0]
    assert row['raw_close'] == '10.00000000' and row['volume_shares'] == '100.00000000'
    assert row['price_basis'] == 'raw' and row['base_date'] is None
    assert 'adj_close' not in row and 'adj_factor_version' not in row


def test_adjusted_basis_requires_the_snapshot_factor_set_and_matching_base(lab):
    from app.services.governance_prices import PriceService, install_price_schema
    install_price_schema(lab.engine)
    lab.at(datetime(2026, 6, 2, 8, tzinfo=timezone.utc))
    version = lab.daily('2026-06-02', published_at='2026-06-02T16:00:00+08:00')
    lab.at(datetime(2026, 6, 2, 9, 30, tzinfo=timezone.utc))
    prices = PriceService(lab.writer)
    spec = {'instrument_id': lab.identity, 'instrument_version': lab.instrument_version,
            'calendar_version': lab.calendar_version, 'base_date': '2026-06-02',
            'as_of': '2026-06-02T09:00:00Z', 'knowledge_mode': 'system_known',
            'algorithm_version': 'r1-qfq-v1', 'raw_data_versions': [version], 'event_inputs': []}
    factor = prices.freeze_factors(spec, evidence=b'SYNTHETIC M08.1 selected dependent versions')
    prices.derive(version, factor)

    plain = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                        created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1))
    bound = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                        created_at='2026-06-03T01:00:00.000000Z', published_at=datetime(2026, 6, 3, 2),
                        dependencies=lab.dependencies(adjustment_sets=[factor]))

    def read(snapshot, base_date):
        return fresh(lab).read_as_of(read_request(
            'daily_bars', selector=instruments([lab.identity]), start='2026-06-01', end='2026-06-03',
            as_of='2026-06-02T09:00:00Z', mode='system_known', snapshot=snapshot,
            profile='daily_bars/core', required=DAILY_REQUIRED, price_basis='adj', base_date=base_date))

    unbound = read(plain, '2026-06-02')
    assert unbound['rows'] == [] and 'ADJUSTMENT_UNAVAILABLE' in reasons(unbound)
    blocked = [item for item in unbound['missing'] if item.get('event_date') == '2026-06-02']
    assert blocked and blocked[0]['reason'] == 'ADJUSTMENT_UNAVAILABLE'

    adjusted = read(bound, '2026-06-02')['rows'][0]
    assert adjusted['adj_close'] == '10.00000000' and adjusted['adj_factor_version'] == factor
    assert adjusted['base_date'] == '2026-06-02' and adjusted['price_basis'] == 'adj'
    assert adjusted['volume_shares'] == '100.00000000'

    with pytest.raises(DataAccessError) as exc:
        read(bound, '2026-06-01')
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'BASE_DATE_MISMATCH'


def test_factor_set_older_than_the_cutoff_is_usable_and_a_later_one_is_not(lab):
    """4.1: factor knowledge is bounded by as_of, not by an exact timestamp match."""
    from app.services.governance_prices import PriceService, install_price_schema
    install_price_schema(lab.engine)
    lab.at(datetime(2026, 6, 2, 8, tzinfo=timezone.utc))
    version = lab.daily('2026-06-02', published_at='2026-06-02T16:00:00+08:00')
    lab.at(datetime(2026, 6, 2, 9, 30, tzinfo=timezone.utc))
    prices = PriceService(lab.writer)
    base_spec = {'instrument_id': lab.identity, 'instrument_version': lab.instrument_version,
                 'calendar_version': lab.calendar_version, 'base_date': '2026-06-02',
                 'knowledge_mode': 'system_known', 'algorithm_version': 'r1-qfq-v1',
                 'raw_data_versions': [version], 'event_inputs': []}
    early = prices.freeze_factors({**base_spec, 'as_of': '2026-06-02T09:00:00Z'},
                                  evidence=b'SYNTHETIC early factor set')
    prices.derive(version, early)
    lab.at(datetime(2026, 6, 2, 11, 30, tzinfo=timezone.utc))
    late = prices.freeze_factors({**base_spec, 'as_of': '2026-06-02T11:00:00Z'},
                                 evidence=b'SYNTHETIC later factor set')
    prices.derive(version, late)
    assert early != late

    early_snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                                 created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1),
                                 dependencies=lab.dependencies(adjustment_sets=[early]))
    late_snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                                created_at='2026-06-03T02:00:00.000000Z', published_at=datetime(2026, 6, 3, 3),
                                dependencies=lab.dependencies(adjustment_sets=[late]))
    both_snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                                created_at='2026-06-03T04:00:00.000000Z', published_at=datetime(2026, 6, 3, 5),
                                dependencies=lab.dependencies(adjustment_sets=[early, late]))

    def read(snapshot, as_of):
        return fresh(lab).read_as_of(read_request(
            'daily_bars', selector=instruments([lab.identity]), start='2026-06-02', end='2026-06-03',
            as_of=as_of, mode='system_known', snapshot=snapshot, profile='daily_bars/core',
            required=DAILY_REQUIRED, price_basis='adj', base_date='2026-06-02'))

    served = read(early_snapshot, '2026-06-02T10:00:00Z')
    assert [row['adj_factor_version'] for row in served['rows']] == [early]
    refused = read(late_snapshot, '2026-06-02T10:00:00Z')
    assert refused['rows'] == [] and reasons(refused) == ['ADJUSTMENT_UNAVAILABLE']

    # Both sets are members: the newest one known at the cutoff wins, per read.
    assert [row['adj_factor_version'] for row in read(both_snapshot, '2026-06-02T09:00:00Z')['rows']] == [early]
    assert [row['adj_factor_version'] for row in read(both_snapshot, '2026-06-02T11:00:00Z')['rows']] == [late]


def test_missing_trading_day_keeps_its_slot_and_is_not_compressed(lab):
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    lab.daily('2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    lab.at(datetime(2026, 6, 3, 8, tzinfo=timezone.utc))
    lab.daily('2026-06-03', published_at='2026-06-03T16:00:00+08:00')
    snapshot = lab.publish({'daily_bars': []}, profiles=all_profiles(),
                           created_at='2026-06-04T00:00:00.000000Z', published_at=datetime(2026, 6, 4, 1))
    # Members are the two written facts; the published set is rebuilt below from the real rows.
    with lab.engine.connect() as db:
        members = [row for row in db.execute(select(s.fact_versions.c.data_version)
                                             .where(s.fact_versions.c.dataset == 'daily_bars')).scalars()]
    snapshot = lab.publish({'daily_bars': members}, profiles=all_profiles(),
                           created_at='2026-06-04T01:00:00.000000Z', published_at=datetime(2026, 6, 4, 2))
    result = fresh(lab).read_as_of(read_request(
        'daily_bars', selector=instruments([lab.identity]), start='2026-06-01', end='2026-06-04',
        as_of='2026-06-04T09:00:00+08:00', mode='system_known', snapshot=snapshot,
        profile='daily_bars/core', required=DAILY_REQUIRED))
    assert [row['event_date'] for row in result['rows']] == ['2026-06-01', '2026-06-03']
    assert [(item['entity_key'], item['event_date'], item['reason'], item['detail']) for item in result['missing']] == [
        (lab.identity, '2026-06-02', 'NOT_INGESTED', 'MISSING_TRADING_DAY')]


# ------------------------------------------------------------ read-only contract
def test_reads_are_repeatable_and_leave_governance_state_untouched(lab):
    snapshot, _ = basic(lab)
    before = dump(lab.engine)
    first = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    second = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert canonical(first) == canonical(second)
    assert dump(lab.engine) == before


def test_read_only_connection_can_serve_the_query(lab):
    snapshot, _ = basic(lab)
    reader = create_engine(lab.engine.url)

    @event.listens_for(reader, 'connect')
    def _read_only(dbapi, record):  # noqa: ANN001
        cursor = dbapi.cursor()
        cursor.execute('PRAGMA query_only=ON' if reader.dialect.name == 'sqlite'
                       else 'SET SESSION TRANSACTION READ ONLY')
        cursor.close()

    result = DataAccessService(reader, clock=lambda: NOW).read_as_of(
        fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert result['rows'][0]['net_profit_parent'] == '100.00000000'
    reader.dispose()


# --------------------------------------------------------------- request contract
@pytest.mark.parametrize('case,detail', [
    ('drop_field', 'REQUEST_FIELDS'),
    ('extra_field', 'REQUEST_FIELDS'),
    ('naive_as_of', 'TIME_OFFSET_REQUIRED'),
    ('future_as_of', 'AS_OF_FUTURE'),
    ('latest_snapshot', 'SNAPSHOT_ID_INVALID'),
    ('bad_mode', 'KNOWLEDGE_MODE_INVALID'),
    ('bad_dataset', 'DATASET_UNKNOWN'),
    ('selector_mismatch', 'SELECTOR_DATASET_MISMATCH'),
    ('selector_fields', 'SELECTOR_FIELDS'),
    ('empty_ids', 'INSTRUMENT_IDS_EMPTY'),
    ('duplicate_ids', 'INSTRUMENT_IDS_DUPLICATE'),
    ('bool_id', 'INSTRUMENT_ID_INVALID'),
    ('range_empty', 'EVENT_RANGE_EMPTY'),
    ('range_fields', 'EVENT_RANGE_FIELDS'),
    ('adj_without_base', 'BASE_DATE_REQUIRED'),
    ('adj_on_financial', 'PRICE_BASIS_NOT_APPLICABLE'),
    ('base_with_raw', 'BASE_DATE_NOT_APPLICABLE'),
])
def test_request_envelope_is_strict(lab, case, detail):
    snapshot, _ = basic(lab)
    if case in {'adj_without_base', 'base_with_raw'}:
        # price_basis only applies to daily_bars; the request fails before any snapshot read.
        request = read_request('daily_bars', selector=instruments([lab.identity]), start='2026-06-01',
                               end='2026-06-03', as_of='2026-06-02T17:00:00+08:00', mode='system_known',
                               snapshot='a' * 64, profile='daily_bars/core', required=DAILY_REQUIRED)
    else:
        request = fin(lab, snapshot, '2026-04-30T15:00:00+08:00')
    if case == 'drop_field':
        request.pop('base_date')
    elif case == 'extra_field':
        request['unexpected'] = 1
    elif case == 'naive_as_of':
        request['as_of'] = '2026-04-30T15:00:00'
    elif case == 'future_as_of':
        request['as_of'] = '2030-01-01T00:00:00+08:00'
    elif case == 'latest_snapshot':
        request['snapshot_id'] = 'latest'
    elif case == 'bad_mode':
        request['knowledge_mode'] = 'mixed'
    elif case == 'bad_dataset':
        request['dataset'] = 'unknown_dataset'
    elif case == 'selector_mismatch':
        request['selector'] = {'kind': 'index', 'index_ids': ['CSI:000300']}
    elif case == 'selector_fields':
        request['selector'] = {'kind': 'instrument', 'instrument_ids': [lab.identity], 'authority': 'SSE'}
    elif case == 'empty_ids':
        request['selector'] = {'kind': 'instrument', 'instrument_ids': []}
    elif case == 'duplicate_ids':
        request['selector'] = {'kind': 'instrument', 'instrument_ids': [lab.identity, lab.identity]}
    elif case == 'bool_id':
        request['selector'] = {'kind': 'instrument', 'instrument_ids': [True]}
    elif case == 'range_empty':
        request['event_range'] = {'start': '2026-04-30', 'end_exclusive': '2026-04-30'}
    elif case == 'range_fields':
        request['event_range'] = {'start': '2026-04-30'}
    elif case == 'adj_without_base':
        request['price_basis'] = 'adj'
    elif case == 'adj_on_financial':
        request['price_basis'] = 'adj'
        request['base_date'] = '2026-04-30'
    elif case == 'base_with_raw':
        request['price_basis'] = 'raw'
        request['base_date'] = '2026-04-30'
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(request)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.status == 400
    assert exc.value.detail == detail


# --------------------------------------------------- publication-time boundaries
def test_fixed_snapshot_stays_replayable_after_its_publication(lab):
    """R08/14.3: publication is not a knowledge ceiling for a fixed snapshot.

    Reading S1 after it was published must still return exactly S1's members: the
    snapshot bounds the candidate set, ``as_of`` bounds knowledge, and a fact written
    afterwards is missing rather than silently served through a current-pointer fallback.
    """
    snapshot, version = basic(lab)
    at_publication = fresh(lab).read_as_of(fin(lab, snapshot, '2026-05-01T09:00:00+08:00'))
    assert at_publication['rows'][0]['net_profit_parent'] == '100.00000000'

    lab.at(datetime(2026, 5, 4, 2, tzinfo=timezone.utc))
    lab.financial('2026-03-31', '55', published_at='2026-05-04T10:00:00+08:00', revision_order=2,
                  supersedes=version, acceptance_event='T09', revision=1)
    later = fresh(lab).read_as_of(fin(lab, snapshot, '2026-05-05T15:00:00+08:00'))
    assert [row['data_version'] for row in later['rows']] == [version]
    assert later['rows'][0]['net_profit_parent'] == '100.00000000'

    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2030-01-01T00:00:00+08:00'))
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'AS_OF_FUTURE'


def test_uppercase_or_non_hex_snapshot_ids_are_request_errors(lab):
    snapshot, _ = basic(lab)
    for value in ('A' * 64, snapshot[:-1], 'z' * 64):
        with pytest.raises(DataAccessError) as exc:
            fresh(lab).read_as_of(fin(lab, value, '2026-04-30T15:00:00+08:00'))
        assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'SNAPSHOT_ID_INVALID'


def test_factor_set_is_never_reused_across_knowledge_modes(lab):
    from app.services.governance_prices import PriceService, install_price_schema
    install_price_schema(lab.engine)
    lab.at(datetime(2026, 6, 2, 8, tzinfo=timezone.utc))
    version = lab.daily('2026-06-02', published_at='2026-06-02T16:00:00+08:00')
    lab.at(datetime(2026, 6, 2, 9, 30, tzinfo=timezone.utc))
    prices = PriceService(lab.writer)
    spec = {'instrument_id': lab.identity, 'instrument_version': lab.instrument_version,
            'calendar_version': lab.calendar_version, 'base_date': '2026-06-02',
            'as_of': '2026-06-02T09:00:00Z', 'knowledge_mode': 'system_known',
            'algorithm_version': 'r1-qfq-v1', 'raw_data_versions': [version], 'event_inputs': []}
    factor = prices.freeze_factors(spec, evidence=b'SYNTHETIC system-mode factor set')
    prices.derive(version, factor)
    snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1),
                           dependencies=lab.dependencies(adjustment_sets=[factor]))
    result = fresh(lab).read_as_of(read_request(
        'daily_bars', selector=instruments([lab.identity]), start='2026-06-02', end='2026-06-03',
        as_of='2026-06-02T09:00:00Z', mode='public_known', snapshot=snapshot,
        profile='daily_bars/core', required=DAILY_REQUIRED, price_basis='adj', base_date='2026-06-02'))
    assert result['rows'] == [] and reasons(result) == ['ADJUSTMENT_UNAVAILABLE']


def test_range_of_closed_days_reports_no_fake_slots(lab):
    _, version = basic(lab)
    lab.at(datetime(2026, 6, 5, 8, tzinfo=timezone.utc))
    daily = lab.daily('2026-06-05', published_at='2026-06-05T16:00:00+08:00')
    weekend = lab.publish({'daily_bars': [daily]}, profiles=all_profiles(),
                          created_at='2026-06-08T00:00:00.000000Z', published_at=datetime(2026, 6, 8, 1))
    result = fresh(lab).read_as_of(read_request(
        'daily_bars', selector=instruments([lab.identity]), start='2026-06-06', end='2026-06-08',
        as_of='2026-06-08T09:00:00+08:00', mode='system_known', snapshot=weekend,
        profile='daily_bars/core', required=DAILY_REQUIRED))
    assert result['rows'] == [] and result['missing'] == []
    assert version != daily
