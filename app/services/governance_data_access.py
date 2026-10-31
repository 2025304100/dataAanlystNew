"""The single formal as-of read service (M08.1). Read-only, no HTTP route.

Every read is first bounded by one published snapshot manifest, then selects one
accepted version per logical key with the R2 rules. There is no "latest" input,
no implicit current-pointer fallback, no candidate auxiliary reader and no write:
facts, availability, acceptance, current and pointer tables are never mutated.
M08.2/3 reuse the same manifest format for building and publishing.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import governance_schema as s
from app.services.data_contract_errors import DataContractError
from app.services.data_contract_profiles import PROFILES, _valid, assess_profile, get_profile
from app.services.governance_fact_contracts import WriterError, canonical, digest, day, hash_object, instant
from app.services.governance_master_data import MasterDataError, MasterDataService
from app.services.governance_snapshot_manifest import (
    DATASETS, INDEX_CALENDAR, SnapshotManifestError, acceptance_hash, dataset_identity, frozen_acceptance,
    is_hash, load_document, manifest_hash, session_close, snapshot_identity,
)

CORE_PROFILES = frozenset(profile.profile_id for profile in PROFILES)


SHANGHAI = ZoneInfo('Asia/Shanghai')
TOP_FIELDS = frozenset({'dataset', 'selector', 'event_range', 'as_of', 'knowledge_mode', 'snapshot_id',
                        'quality_profile', 'required_fields', 'price_basis', 'base_date'})
INSTRUMENT_DATASETS = frozenset({'daily_bars', 'valuation', 'financial', 'capital_flow', 'lhb', 'hot_rank',
                                 'etf', 'share_capital', 'corporate_action'})
CALENDAR_DATASETS = frozenset({'daily_bars', 'index'})
CONSUMABLE_STATES = frozenset({'canonical', 'backup_temp'})
REASONS = ('NOT_KNOWN', 'NOT_INGESTED', 'RETRACTED', 'QUALITY_BLOCKED', 'ADJUSTMENT_UNAVAILABLE')
KEY_OUTPUT = {
    'valuation': ('trade_date', 'definition_id'),
    'financial': ('report_period', 'report_type', 'definition_id'),
    'capital_flow': ('trade_date', 'flow_definition'),
    'lhb': ('trade_date', 'reason_scope', 'dedup_rule_id'),
    'hot_rank': ('ranking_method', 'observation_at'),
    'etf': ('valuation_date', 'profile', 'share_class'),
    'index': ('trade_date', 'definition_id'),
    'macro': ('period', 'frequency', 'definition_id'),
    'share_capital': ('effective_date', 'share_class'),
    'corporate_action': ('authority_event_id',),
    'daily_bars': ('trade_date',),
}


class DataAccessError(ValueError):
    """Application-service contract error; no HTTP route is added in M08.1."""

    def __init__(self, code, status, detail=''):
        self.code = code
        self.status = status
        self.detail = detail
        super().__init__(code + (':' + detail if detail else ''))

    @property
    def http_status(self):
        """Contract spelling (8.2); ``status`` is kept as the alias callers already use."""
        return self.status


def _reject(code, detail=''):
    raise DataAccessError(code, 400, detail)


def _conflict(code, detail=''):
    raise DataAccessError(code, 409, detail)


def _missing(entity_key, reason, detail='', *, event_date=None, logical_key=None):
    """One per-key missing record; the reason enum is enforced, never free text."""
    if reason not in REASONS:
        raise DataAccessError('INVALID_ARGUMENT', 400, 'MISSING_REASON_INVALID:' + str(reason))
    record = {'entity_key': entity_key, 'reason': reason}
    if event_date is not None:
        record['event_date'] = event_date
    if detail:
        record['detail'] = detail
    if logical_key is not None:
        record['logical_key'] = logical_key
    return record


def _entity_order(value):
    """Numeric ids sort numerically; string namespaces sort after ids, deterministically."""
    if type(value) is int:
        return (0, value, '')
    if value is None:
        return (2, 0, '')
    return (1, 0, str(value))


def _as_utc(value):
    """Stored UTC columns come back naive from both engines; an offset-less value is UTC."""
    return value if value.tzinfo is not None and value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)


def _hashes(value):
    """Every 64-hex literal inside a key/payload: candidate reference to another fact."""
    found = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if is_hash(item):
                found.add(item)
        elif type(item) is dict:
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
    return found


@dataclass(frozen=True)
class PublishedSnapshot:
    snapshot_id: str
    published_at: datetime
    document: dict
    dataset_versions: dict
    members_by_dataset: dict

    @property
    def instrument_version(self):
        return self.document['dependencies']['instrument_version']

    @property
    def closes(self):
        return self.document['dependencies']['session_closes']

    @property
    def adjustment_sets(self):
        return tuple(self.document['dependencies']['adjustment_sets'])

    def entry(self, dataset):
        entry = self.document['datasets'].get(dataset)
        if entry is None:
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'DATASET_NOT_IN_SNAPSHOT:' + dataset)
        return entry

    def members(self, dataset):
        self.entry(dataset)
        return self.members_by_dataset[dataset]

    def base_date(self, as_of, exchange):
        value = session_close(self.closes, as_of, exchange)
        if value is None:
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'BASE_DATE_UNKNOWN:' + exchange)
        return value


class DataAccessService:
    def __init__(self, engine, *, clock=lambda: datetime.now(timezone.utc)):
        self.engine = engine
        self.clock = clock
        self._cache = {}
        self._fact_cache = {}
        self._gate_cache = {}
        self._price_service = None

    # ------------------------------------------------------------------ request
    def read_as_of(self, request):
        parsed = self._request(request)
        # Request-scope rejections never depend on the snapshot: a future `as_of` or an
        # unregistered instrument must stay INVALID_ARGUMENT/400 even when the snapshot
        # id itself is unknown (14.12.3 "未知ID为INVALID_ARGUMENT").
        if parsed['cutoff'] > instant(self.clock()):
            _reject('INVALID_ARGUMENT', 'AS_OF_FUTURE')
        registered = self._registered(parsed)
        snapshot = self._snapshot(parsed['snapshot_id'])
        # R08/14.3: a fixed snapshot stays replayable after its publication. The snapshot
        # bounds the candidate set, ``as_of`` bounds the knowledge cutoff; there is no
        # extra "as_of <= published_at" ceiling and never a fall back to current.
        profile = self._profile(parsed, snapshot)
        instruments = self._instruments(parsed, snapshot, registered)
        base = self._base_date(parsed, snapshot, instruments['exchanges'])
        selection = self._selection(parsed, snapshot, profile, instruments, base)
        rows, missing = selection
        return {'snapshot_id': parsed['snapshot_id'], 'knowledge_mode': parsed['mode'], 'as_of': parsed['as_of_text'],
                'reconstruction': parsed['mode'] == 'public_known', 'rows': rows, 'missing': missing}

    @staticmethod
    def _enum(value, allowed, detail):
        """R10: reject every non-string before any set/mapping lookup (never TypeError)."""
        if not isinstance(value, str) or value not in allowed:
            _reject('INVALID_ARGUMENT', detail)
        return value

    def _request(self, request):
        if type(request) is not dict or set(request) != TOP_FIELDS:
            _reject('INVALID_ARGUMENT', 'REQUEST_FIELDS')
        dataset = self._enum(request['dataset'], DATASETS, 'DATASET_UNKNOWN')
        selector = self._selector(dataset, request['selector'])
        if dataset == 'macro':
            self._macro_registry(selector['indicator_keys'])
        start, end = self._range(request['event_range'])
        as_of_text, aware, cutoff = self._as_of(request['as_of'])
        mode = self._enum(request['knowledge_mode'], {'public_known', 'system_known'}, 'KNOWLEDGE_MODE_INVALID')
        if not is_hash(request['snapshot_id']):
            _reject('INVALID_ARGUMENT', 'SNAPSHOT_ID_INVALID')
        price_basis = self._enum(request['price_basis'], {'raw', 'adj', 'none'}, 'PRICE_BASIS_INVALID')
        if price_basis == 'none':
            if request['base_date'] is not None:
                _reject('INVALID_ARGUMENT', 'BASE_DATE_NOT_APPLICABLE')
            base_date = None
        elif dataset != 'daily_bars':
            _reject('INVALID_ARGUMENT', 'PRICE_BASIS_NOT_APPLICABLE')
        elif price_basis == 'raw':
            if request['base_date'] is not None:
                _reject('INVALID_ARGUMENT', 'BASE_DATE_NOT_APPLICABLE')
            base_date = None
        else:
            if request['base_date'] is None:
                _reject('INVALID_ARGUMENT', 'BASE_DATE_REQUIRED')
            try:
                base_date = day(request['base_date'])
            except (WriterError, TypeError, ValueError):
                _reject('INVALID_ARGUMENT', 'BASE_DATE_INVALID')
        return {'dataset': dataset, 'selector': selector, 'start': start, 'end': end, 'as_of_text': as_of_text,
                'aware': aware, 'cutoff': cutoff, 'mode': mode, 'snapshot_id': request['snapshot_id'],
                'price_basis': price_basis, 'base_date': base_date, 'profile_id': request['quality_profile'],
                'required_fields': self._required(request['required_fields'])}

    @staticmethod
    def _selector(dataset, selector):
        if type(selector) is not dict or 'kind' not in selector:
            _reject('INVALID_ARGUMENT', 'SELECTOR_INVALID')
        kind = DataAccessService._enum(selector['kind'], {'instrument', 'index', 'macro', 'announcement'},
                                       'SELECTOR_INVALID')
        if dataset == 'announcement':
            if kind == 'announcement':
                if set(selector) != {'kind', 'authority', 'announcement_ids'}:
                    _reject('INVALID_ARGUMENT', 'SELECTOR_FIELDS')
                authority = selector['authority']
                if not isinstance(authority, str) or not authority.strip() or len(authority) > 64:
                    _reject('INVALID_ARGUMENT', 'AUTHORITY_INVALID')
                return {'kind': 'announcement', 'authority': authority,
                        'announcement_ids': DataAccessService._strings(selector['announcement_ids'], 'ANNOUNCEMENT_ID_INVALID')}
            if kind == 'instrument':
                return {'kind': 'instrument',
                        'instrument_ids': DataAccessService._instruments_only(selector)}
        wanted = {'instrument': INSTRUMENT_DATASETS, 'index': {'index'}, 'macro': {'macro'}}.get(kind)
        if wanted is None or dataset not in wanted:
            _reject('INVALID_ARGUMENT', 'SELECTOR_DATASET_MISMATCH')
        if kind == 'instrument':
            return {'kind': 'instrument', 'instrument_ids': DataAccessService._instruments_only(selector)}
        if kind == 'index':
            if set(selector) != {'kind', 'index_ids'}:
                _reject('INVALID_ARGUMENT', 'SELECTOR_FIELDS')
            return {'kind': 'index', 'index_ids': DataAccessService._strings(selector['index_ids'], 'INDEX_ID_INVALID')}
        if set(selector) != {'kind', 'indicator_keys'}:
            _reject('INVALID_ARGUMENT', 'SELECTOR_FIELDS')
        return {'kind': 'macro', 'indicator_keys': DataAccessService._strings(selector['indicator_keys'], 'INDICATOR_KEY_INVALID')}

    @staticmethod
    def _instruments_only(selector):
        if set(selector) != {'kind', 'instrument_ids'}:
            _reject('INVALID_ARGUMENT', 'SELECTOR_FIELDS')
        values = selector['instrument_ids']
        if type(values) is not list or not values:
            _reject('INVALID_ARGUMENT', 'INSTRUMENT_IDS_EMPTY')
        if any(type(value) is not int or not 0 < value < 2 ** 63 for value in values):
            _reject('INVALID_ARGUMENT', 'INSTRUMENT_ID_INVALID')
        if len(set(values)) != len(values):
            _reject('INVALID_ARGUMENT', 'INSTRUMENT_IDS_DUPLICATE')
        return tuple(values)

    @staticmethod
    def _strings(values, detail):
        if type(values) is not list or not values:
            _reject('INVALID_ARGUMENT', detail)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in values):
            _reject('INVALID_ARGUMENT', detail)
        if len(set(values)) != len(values):
            _reject('INVALID_ARGUMENT', detail + '_DUPLICATE')
        return tuple(values)

    @staticmethod
    def _macro_registry(indicator_keys):
        """Registered products only; an unregistered key is a request error, not missing."""
        from app.services.governance_macro import FRED_PRODUCTS, OFFICIAL_PRODUCTS
        registered = {value[1] for value in OFFICIAL_PRODUCTS.values()} | {value[0] for value in FRED_PRODUCTS.values()}
        if set(indicator_keys) - registered:
            _reject('INVALID_ARGUMENT', 'INDICATOR_UNREGISTERED')

    @staticmethod
    def _range(value):
        if type(value) is not dict or set(value) != {'start', 'end_exclusive'}:
            _reject('INVALID_ARGUMENT', 'EVENT_RANGE_FIELDS')
        try:
            start, end = day(value['start']), day(value['end_exclusive'])
        except (WriterError, TypeError, ValueError):
            _reject('INVALID_ARGUMENT', 'EVENT_RANGE_INVALID')
        if start >= end:
            _reject('INVALID_ARGUMENT', 'EVENT_RANGE_EMPTY')
        return start, end

    @staticmethod
    def _as_of(value):
        if isinstance(value, datetime):
            aware = value
        elif isinstance(value, str):
            try:
                aware = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                _reject('INVALID_ARGUMENT', 'AS_OF_INVALID')
        else:
            _reject('INVALID_ARGUMENT', 'AS_OF_INVALID')
        if aware.tzinfo is None or aware.utcoffset() is None:
            _reject('INVALID_ARGUMENT', 'TIME_OFFSET_REQUIRED')
        return aware.isoformat(), aware, instant(aware)

    @staticmethod
    def _required(value):
        if type(value) is not list or not value:
            _reject('INVALID_ARGUMENT', 'REQUIRED_FIELDS_EMPTY')
        if any(not isinstance(field, str) or not field for field in value):
            _reject('INVALID_ARGUMENT', 'REQUIRED_FIELDS_INVALID')
        if len(set(value)) != len(value):
            _reject('INVALID_ARGUMENT', 'REQUIRED_FIELDS_DUPLICATE')
        return tuple(value)

    # ----------------------------------------------------------------- snapshot
    def _snapshot(self, snapshot_id):
        if snapshot_id in self._cache:
            return self._cache[snapshot_id]
        with self.engine.connect() as db:
            row = db.execute(select(s.snapshot_manifest).where(s.snapshot_manifest.c.snapshot_id == snapshot_id)).mappings().first()
            if row is None or row['state'] != 'published' or row['published_at'] is None:
                _conflict('SNAPSHOT_NOT_PUBLISHED', 'STATE_NOT_PUBLISHED')
            text = row['manifest_json']
            if digest(text.encode('utf-8')) != row['manifest_hash']:
                _conflict('SNAPSHOT_NOT_PUBLISHED', 'MANIFEST_HASH_MISMATCH')
            try:
                document = load_document(text)
            except SnapshotManifestError as error:
                _conflict('SNAPSHOT_NOT_PUBLISHED', error.code + (':' + error.detail if error.detail else ''))
            if snapshot_identity(document) != snapshot_id:
                _conflict('SNAPSHOT_NOT_PUBLISHED', 'SNAPSHOT_ID_MISMATCH')
            bound = {r['dataset']: r['dataset_version'] for r in db.execute(
                select(s.snapshot_datasets).where(s.snapshot_datasets.c.snapshot_id == snapshot_id)).mappings()}
            expected = {name: entry['dataset_version'] for name, entry in document['datasets'].items()}
            if bound != expected:
                _conflict('SNAPSHOT_NOT_PUBLISHED', 'DATASET_BINDING_MISMATCH')
            members_by_dataset = {}
            for name, version in expected.items():
                stored = db.execute(select(s.dataset_versions).where(s.dataset_versions.c.dataset_version == version)).mappings().first()
                if stored is None or stored['dataset'] != name:
                    _conflict('SNAPSHOT_NOT_PUBLISHED', 'DATASET_VERSION_MISSING:' + name)
                entry = document['datasets'][name]
                if stored['member_hash'] != entry['member_hash'] or stored['acceptance_history_hash'] != entry['acceptance_history_hash']:
                    _conflict('SNAPSHOT_NOT_PUBLISHED', 'MEMBER_HASH_MISMATCH:' + name)
                members = frozenset(db.execute(select(s.dataset_members.c.data_version)
                                               .where(s.dataset_members.c.dataset_version == version)).scalars())
                if len(members) != entry['member_count'] or self._member_hash(name, members) != entry['member_hash']:
                    _conflict('SNAPSHOT_NOT_PUBLISHED', 'MEMBER_HASH_MISMATCH:' + name)
                # R02: the dataset version must be the content identity the manifest claims
                # (members + frozen acceptance + build time), and the stored row must carry
                # the same contract/policy bindings the published manifest certifies.
                if dataset_identity(dataset=name, members=members,
                                    acceptance_history_hash=entry['acceptance_history_hash'],
                                    created_at=document['created_at']) != entry['dataset_version']:
                    _conflict('SNAPSHOT_NOT_PUBLISHED', 'DATASET_VERSION_IDENTITY_MISMATCH:' + name)
                if stored['contract_versions'] != entry['contract_versions'] or \
                        list(stored['source_policy_versions'] or []) != list(entry['source_policy_versions']):
                    _conflict('SNAPSHOT_NOT_PUBLISHED', 'DATASET_BINDING_MISMATCH:' + name)
                members_by_dataset[name] = members
        snapshot = PublishedSnapshot(snapshot_id=snapshot_id, published_at=row['published_at'], document=document,
                                     dataset_versions=expected, members_by_dataset=members_by_dataset)
        self._check_dependencies(snapshot, members_by_dataset)
        self._cache[snapshot_id] = snapshot
        return snapshot

    @staticmethod
    def _member_hash(dataset, members):
        from app.services.governance_snapshot_manifest import member_hash
        return member_hash(dataset, members)

    def _check_dependencies(self, snapshot, members_by_dataset):
        master = MasterDataService(self.engine)
        try:
            master.instrument_snapshot(snapshot.instrument_version)
        except (MasterDataError, WriterError, ValueError):
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'INSTRUMENT_VERSION_UNAVAILABLE')
        for version in snapshot.document['dependencies']['calendar_versions']:
            try:
                master.calendar_snapshot(version)
            except (MasterDataError, WriterError, ValueError):
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'CALENDAR_VERSION_UNAVAILABLE')
        sessions = snapshot.closes['calendar_version']
        if sessions not in snapshot.document['dependencies']['calendar_versions']:
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'SESSION_CLOSES_CALENDAR_MISSING')
        document = master.calendar_snapshot(sessions)
        first, last = day(document['start']), day(document['end_exclusive'])
        for exchange, closes in snapshot.closes['closes'].items():
            try:
                expected = master.trading_days(sessions, 'SSE' if exchange == INDEX_CALENDAR else exchange, first, last)
            except MasterDataError:
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'SESSION_CLOSE_CALENDAR:' + exchange)
            if set(closes) != {value.isoformat() for value in expected}:
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'SESSION_CLOSE_COVERAGE:' + exchange)
        for dataset, members in members_by_dataset.items():
            self._bindings(snapshot, dataset, self._member_facts(snapshot, dataset))
        if snapshot.adjustment_sets:
            with self.engine.connect() as db:
                found = set(db.execute(select(s.adjustment_sets.c.adj_factor_version)
                                       .where(s.adjustment_sets.c.adj_factor_version.in_(snapshot.adjustment_sets))).scalars())
            if found != set(snapshot.adjustment_sets):
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'ADJUSTMENT_SET_MISSING')

    def _member_facts(self, snapshot, dataset):
        # Cache is scoped to one published snapshot: a shared instance must never
        # mix member sets between S1 and S2 (G12 / 8.2-1).
        cache_key = (snapshot.snapshot_id, dataset)
        cached = self._fact_cache.get(cache_key)
        if cached is not None:
            return cached
        rows = {}
        ids = sorted(snapshot.members(dataset))
        with self.engine.connect() as db:
            for start in range(0, len(ids), 500):
                for row in db.execute(select(s.fact_versions)
                                      .where(s.fact_versions.c.data_version.in_(ids[start:start + 500]))).mappings():
                    rows[row['data_version']] = dict(row)
            availability = {r['data_version']: r['available_at'] for r in db.execute(
                select(s.version_availability).where(s.version_availability.c.data_version.in_(ids))).mappings()}
        if set(rows) != set(ids) or any(row['dataset'] != dataset for row in rows.values()):
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'MEMBER_FACT_MISSING:' + dataset)
        for version, row in rows.items():
            try:
                if digest(row['key_json'].encode('utf-8')) != row['logical_key']:
                    raise ValueError()
                if digest(row['payload_json'].encode('utf-8')) != row['normalized_payload_hash']:
                    raise ValueError()
                identity = {name: row[name] for name in ('dataset', 'logical_key', 'source_tag', 'contract_version',
                                                          'definition_id', 'source_revision_token',
                                                          'normalized_payload_hash', 'publication_evidence_hash',
                                                          'supersedes_version')}
                if hash_object('fact-v1', identity) != version:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, AttributeError):
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'FACT_INTEGRITY:' + version)
            row['key'] = json.loads(row['key_json'])
            row['payload'] = json.loads(row['payload_json'])
            row['available_at'] = availability.get(version)
        self._fact_cache[cache_key] = rows
        return rows

    @staticmethod
    def _bindings(snapshot, dataset, facts):
        """R02: every member's unit contract and definition identity must be bound."""
        entry = snapshot.entry(dataset)
        contracts = entry['contract_versions']
        definitions = set(entry['definition_versions'])
        for row in facts.values():
            if row['contract_version'] not in contracts:
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'CONTRACT_NOT_BOUND:' + dataset)
            if row['definition_id'] not in definitions:
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'DEFINITION_NOT_BOUND:' + dataset)

    def _closure(self, snapshot, dataset, facts):
        """R03: what a member fact points at must be inside the snapshot, not merely in the库.

        The check is scoped to the dataset being read: a published snapshot that is
        incomplete for another dataset must not silently certify this one.
        """
        members = frozenset().union(*snapshot.members_by_dataset.values()) if snapshot.members_by_dataset else frozenset()
        candidates = set()
        for row in facts.values():
            if row['supersedes_version'] and row['supersedes_version'] not in members:
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'SUPERSEDES_OUTSIDE_SNAPSHOT')
            candidates |= _hashes(row['key']) | _hashes(row['payload'])
        outside = self._known_versions(value for value in candidates if value not in members)
        if outside:
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'REFERENCE_OUTSIDE_SNAPSHOT:' + sorted(outside)[0])

    def _known_versions(self, candidates):
        ids = sorted(set(candidates))
        if not ids:
            return frozenset()
        found = set()
        with self.engine.connect() as db:
            for start in range(0, len(ids), 500):
                found |= set(db.execute(select(s.fact_versions.c.data_version)
                                        .where(s.fact_versions.c.data_version.in_(ids[start:start + 500]))).scalars())
        return frozenset(found)

    # ---------------------------------------------------------------- selection
    def _selection(self, parsed, snapshot, profile, instruments, base):
        dataset = parsed['dataset']
        members = snapshot.members(dataset)
        facts = self._member_facts(snapshot, dataset)
        self._closure(snapshot, dataset, facts)
        histories = self._histories(snapshot, dataset, members)
        candidates = self._candidates(parsed, snapshot, facts)
        chosen = self._choose(parsed, candidates, histories, members)
        rows, missing = [], []
        for logical_key in sorted(chosen):
            row, record = chosen[logical_key]
            entity = self._entity_key(parsed, row)
            if row['revision_kind'] == 'withdrawal':
                missing.append(_missing(entity, 'RETRACTED', 'WITHDRAWN_VERSION_VISIBLE',
                                        event_date=row['event_date'].isoformat(), logical_key=logical_key))
                continue
            record = record or self._latest_acceptance(histories.get(row['data_version'], ()))
            if record is None:
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'ACCEPTANCE_MISSING')
            if record['quality_status'] == 'quarantined':
                missing.append(_missing(entity, 'QUALITY_BLOCKED', 'QUALITY_QUARANTINED',
                                        event_date=row['event_date'].isoformat(), logical_key=logical_key))
                continue
            if record['source_state'] not in CONSUMABLE_STATES:
                missing.append(_missing(entity, 'QUALITY_BLOCKED',
                                        'SOURCE_STATE_NOT_CONSUMABLE:' + record['source_state'],
                                        event_date=row['event_date'].isoformat(), logical_key=logical_key))
                continue
            gates = self._gates(record['decision_id'])
            try:
                # R05: fields the caller declares required are required for this read, even
                # when the frozen profile lists them as optional.
                request_quality, detail = self._request_quality(row['payload'], profile, parsed['required_fields'], gates)
                core_quality = self._dataset_quality(row['payload'], dataset, profile, gates)
            except DataContractError:
                missing.append(_missing(entity, 'QUALITY_BLOCKED', 'PROFILE_PAYLOAD_INVALID',
                                        event_date=row['event_date'].isoformat(), logical_key=logical_key))
                continue
            if request_quality != 'complete':
                missing.append(_missing(entity, 'QUALITY_BLOCKED', detail,
                                        event_date=row['event_date'].isoformat(), logical_key=logical_key))
                continue
            prices = None
            if parsed['price_basis'] == 'adj':
                try:
                    prices = self._adjusted(snapshot, parsed, row, base)
                except DataAccessError as error:
                    if error.code != 'INVALID_ARGUMENT' and error.detail.startswith('ADJUSTMENT'):
                        missing.append(_missing(entity, 'ADJUSTMENT_UNAVAILABLE', error.detail,
                                                event_date=row['event_date'].isoformat(), logical_key=logical_key))
                        continue
                    raise
            rows.append(self._row(parsed, profile, row, record, core_quality, request_quality, prices))
        missing.extend(self._absent(parsed, snapshot, instruments, rows, missing))
        rows.sort(key=lambda item: (item['event_date'],
                                    _entity_order(item.get('instrument_id') if item.get('instrument_id') is not None
                                                  else item.get('index_id') if item.get('index_id') is not None
                                                  else item.get('indicator_key') if item.get('indicator_key') is not None
                                                  else item.get('announcement_id')),
                                    item['logical_key']))
        missing.sort(key=lambda item: (_entity_order(item['entity_key']), item.get('event_date') or '', item['reason']))
        return rows, missing

    def _histories(self, snapshot, dataset, members):
        """R01: read the history the snapshot froze, and prove it is still what was promised.

        The frozen projection excludes ``system_to`` (a later supersession legitimately
        closes an interval) and rows accepted after ``created_at``; everything the reader
        serves is in it, so a live provenance change is a dependency failure instead of a
        silently different row.
        """
        ids = sorted(members)
        live = []
        with self.engine.connect() as db:
            for start in range(0, len(ids), 500):
                for row in db.execute(select(s.acceptance_history)
                                      .where(s.acceptance_history.c.data_version.in_(ids[start:start + 500]))
                                      .order_by(s.acceptance_history.c.pointer_revision)).mappings():
                    live.append(dict(row))
        created_at = snapshot.document['created_at']
        if acceptance_hash(dataset, frozen_acceptance(live, created_at)) != snapshot.entry(dataset)['acceptance_history_hash']:
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'ACCEPTANCE_HISTORY_MISMATCH:' + dataset)
        cutoff = instant(created_at)
        rows = {}
        for row in live:
            start = row['system_from']
            if start is None:
                continue
            if instant(_as_utc(start)) > cutoff:
                continue
            rows.setdefault(row['data_version'], []).append(row)
        return rows

    @staticmethod
    def _latest_acceptance(records):
        return max(records, key=lambda row: row['pointer_revision']) if records else None

    @staticmethod
    def _request_quality(payload, profile, required_fields, gates):
        assessment = assess_profile(payload, profile.profile_id, gate_results=gates)
        if assessment.quality_status != 'complete':
            return 'partial', 'REQUEST_PROFILE_INCOMPLETE:' + profile.profile_id
        for field in required_fields:
            if field in profile.required_fields:
                continue
            value = payload.get(field)
            if value is None:
                return 'partial', 'REQUEST_FIELD_MISSING:' + field
            if not _valid(field, value):
                return 'partial', 'REQUEST_FIELD_INVALID:' + field
        return 'complete', None

    @staticmethod
    def _dataset_quality(payload, dataset, profile, gates):
        """R04: a product family without a ``<dataset>/core`` profile (etf) must not be
        blocked by a core profile that does not exist; its own product profile is then the
        dataset-level contract."""
        core_id = dataset + '/core'
        target = core_id if core_id in CORE_PROFILES else profile.profile_id
        return assess_profile(payload, target, gate_results=gates).quality_status

    def _candidates(self, parsed, snapshot, facts):
        selector = parsed['selector']
        if parsed['dataset'] == 'announcement':
            # Announcement keys carry no instrument: only the snapshot's explicit links apply.
            if 'authority' in selector:
                wanted = set(selector['announcement_ids'])
                keep = lambda row: row['key'].get('authority') == selector['authority'] and row['key'].get('announcement_id') in wanted  # noqa: E731
            else:
                links = self._announcement_links(snapshot, selector['instrument_ids'])
                requested = [str(value) for value in selector['instrument_ids']]
                if not any(value in links for value in requested):
                    return {}
                wanted = {item for value in requested for item in links.get(value, ())}
                keep = lambda row: row['key'].get('announcement_id') in wanted  # noqa: E731
        elif selector['kind'] == 'instrument':
            wanted = set(selector['instrument_ids'])
            keep = lambda row: row['key'].get('instrument_id') in wanted  # noqa: E731
        elif selector['kind'] == 'index':
            wanted = set(selector['index_ids'])
            keep = lambda row: row['key'].get('index_id') in wanted  # noqa: E731
        else:
            wanted = set(selector['indicator_keys'])
            keep = lambda row: row['key'].get('indicator_key') in wanted  # noqa: E731
        groups = {}
        for row in facts.values():
            if not keep(row):
                continue
            if not parsed['start'] <= row['event_date'] < parsed['end']:
                continue
            groups.setdefault(row['logical_key'], []).append(row)
        return groups

    def _choose(self, parsed, candidates, histories, members):
        mode, cutoff = parsed['mode'], parsed['cutoff']
        chosen = {}
        for logical_key in sorted(candidates):
            rows = candidates[logical_key]
            if mode == 'public_known':
                # R07: lineage is proved by the whole supersedes chain, including versions
                # that were never publicly visible. A non-public intermediate keeps its
                # parent link but is never emitted; a fork or cycle is still an error.
                by_id = {row['data_version']: row for row in rows}
                children = {}
                for row in rows:
                    parent = row['supersedes_version']
                    if parent is None:
                        continue
                    if parent not in by_id:
                        _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'SUPERSEDES_OUTSIDE_SNAPSHOT')
                    children.setdefault(parent, []).append(row['data_version'])
                roots = [row for row in rows if row['supersedes_version'] is None]
                if len(roots) != 1:
                    _conflict('VERSION_AMBIGUOUS', 'PUBLIC_ROOT')
                chain, seen, cursor = [], set(), roots[0]
                while cursor is not None:
                    version = cursor['data_version']
                    if version in seen:
                        _conflict('VERSION_AMBIGUOUS', 'CHAIN_CYCLE')
                    seen.add(version)
                    chain.append(cursor)
                    kids = children.get(version, [])
                    if len(kids) > 1:
                        _conflict('VERSION_AMBIGUOUS', 'PUBLIC_BRANCH')
                    cursor = by_id[kids[0]] if kids else None
                if seen != set(by_id):
                    _conflict('VERSION_AMBIGUOUS', 'PUBLIC_BRANCH')
                visible = [row for row in chain if row['public_available_at'] is not None
                           and row['public_available_at'] <= cutoff and row['publication_evidence_hash']]
                if not visible:
                    continue
                chosen[logical_key] = (visible[-1], None)
            else:
                hits = []
                for row in rows:
                    for record in histories.get(row['data_version'], ()):
                        if record['system_from'] <= cutoff and (record['system_to'] is None or cutoff < record['system_to']):
                            hits.append((row, record))
                if len(hits) > 1:
                    _conflict('VERSION_AMBIGUOUS', 'SYSTEM_INTERVAL_OVERLAP')
                if not hits:
                    continue
                row, record = hits[0]
                if row['available_at'] is None or row['available_at'] > cutoff:
                    continue
                chosen[logical_key] = (row, record)
        return chosen

    # -------------------------------------------------------------------- gates
    def _gates(self, decision_id):
        if decision_id in self._gate_cache:
            return self._gate_cache[decision_id]
        with self.engine.connect() as db:
            evidence_hash = db.execute(select(s.source_decisions.c.evidence_hash)
                                       .where(s.source_decisions.c.decision_id == decision_id)).scalar_one_or_none()
            if evidence_hash is None:
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'DECISION_EVIDENCE_MISSING')
            raw = db.execute(select(s.raw_evidence.c.raw_bytes)
                             .where(s.raw_evidence.c.raw_hash == evidence_hash)).scalar_one_or_none()
        if raw is None or digest(raw) != evidence_hash:
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'DECISION_EVIDENCE_MISSING')
        try:
            gates = json.loads(raw)['gates']
        except (ValueError, KeyError, TypeError):
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'DECISION_EVIDENCE_INVALID')
        if type(gates) is not dict or any(value not in {'pass', 'fail', 'insufficient', 'not_applicable'}
                                          for value in gates.values()):
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'DECISION_EVIDENCE_INVALID')
        self._gate_cache[decision_id] = gates
        return gates

    # ------------------------------------------------------------------ outputs
    @staticmethod
    def _entity_key(parsed, row):
        if parsed['dataset'] == 'announcement':
            return row['key']['authority'] + ':' + row['key']['announcement_id']
        if parsed['selector']['kind'] == 'instrument':
            return row['key']['instrument_id']
        if parsed['selector']['kind'] == 'index':
            return row['key']['index_id']
        return row['key']['indicator_key']

    def _row(self, parsed, profile, row, record, core_quality, request_quality, prices):
        output = {'data_version': row['data_version'], 'logical_key': row['logical_key'],
                  'event_date': row['event_date'].isoformat(), 'source_state': record['source_state'],
                  'quality_status': core_quality, 'request_quality_status': request_quality,
                  'published_at': self._instant_text(row['published_at']),
                  'publication_date': row['publication_date'].isoformat() if row['publication_date'] else None,
                  'publication_precision': row['publication_precision'],
                  'public_available_at': self._instant_text(row['public_available_at']),
                  'available_at': self._instant_text(row['available_at']),
                  'degraded': record['source_state'] == 'backup_temp'}
        if parsed['dataset'] == 'announcement':
            output['authority'] = row['key']['authority']
            output['announcement_id'] = row['key']['announcement_id']
        elif parsed['selector']['kind'] == 'instrument':
            output['instrument_id'] = row['key']['instrument_id']
        elif parsed['selector']['kind'] == 'index':
            output['index_id'] = row['key']['index_id']
        else:
            output['indicator_key'] = row['key']['indicator_key']
        for name in KEY_OUTPUT.get(parsed['dataset'], ()):
            if name in row['key'] and name not in output:
                output[name] = row['key'][name]
        names = list(dict.fromkeys(list(profile.required_fields) + list(profile.optional_fields) + list(parsed['required_fields'])))
        if prices is not None:
            output.update(prices)
            output['price_basis'] = 'adj'
            output['base_date'] = parsed['base_date'].isoformat()
            for name in ('volume_shares', 'amount_cny', 'turnover_rate_pct', 'is_suspended', 'synthetic_bar'):
                if name in row['payload']:
                    output[name] = row['payload'][name]
        else:
            if parsed['dataset'] == 'daily_bars':
                output['price_basis'] = parsed['price_basis']
                output['base_date'] = None
            for name in names:
                if name in row['payload']:
                    output[name] = row['payload'][name]
        return output

    @staticmethod
    def _instant_text(value):
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(SHANGHAI).isoformat()

    def _absent(self, parsed, snapshot, instruments, rows, missing):
        dataset = parsed['dataset']
        reason = 'NOT_KNOWN' if parsed['mode'] == 'public_known' else 'NOT_INGESTED'
        decided = {(item['entity_key'], item.get('event_date')) for item in missing}
        seen_entities = {item['entity_key'] for item in missing}
        output = []
        if dataset in CALENDAR_DATASETS:
            master = MasterDataService(self.engine)
            calendar_version = snapshot.closes['calendar_version']
            present = set()
            for item in rows:
                if item.get('instrument_id'):
                    present.add((item['instrument_id'], item['event_date']))
                if item.get('index_id'):
                    present.add((item['index_id'], item['event_date']))
            if dataset == 'daily_bars':
                entities = [(value, 'instrument', instruments['exchanges'][value])
                            for value in parsed['selector']['instrument_ids']]
            else:
                if INDEX_CALENDAR not in snapshot.closes['closes']:
                    _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'INDEX_CALENDAR_MISSING')
                entities = [(value, 'index', master.calendar_snapshot(calendar_version)['exchange'])
                            for value in parsed['selector']['index_ids']]
            for entity, kind, exchange in entities:
                if kind == 'instrument' and entity not in instruments['members']:
                    if (entity, None) not in decided:
                        output.append(_missing(entity, reason, 'INSTRUMENT_NOT_IN_SNAPSHOT'))
                    continue
                try:
                    days = master.trading_days(calendar_version, exchange, parsed['start'], parsed['end'])
                except MasterDataError as error:
                    _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'CALENDAR_RANGE_UNKNOWN:' + str(error))
                expected = 0
                for on in days:
                    if kind == 'instrument':
                        try:
                            master.validate_lifecycle(snapshot.instrument_version, entity, on)
                        except MasterDataError as error:
                            if str(error) == 'OUTSIDE_LIFECYCLE':
                                continue
                            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'INSTRUMENT_VERSION_INVALID')
                    expected += 1
                    if (entity, on.isoformat()) in present or (entity, on.isoformat()) in decided:
                        continue
                    output.append(_missing(entity, reason, 'MISSING_TRADING_DAY', event_date=on.isoformat()))
                if expected == 0 and days:
                    output.append(_missing(entity, reason, 'OUTSIDE_LIFECYCLE'))
            return output
        if dataset == 'announcement' and 'authority' not in parsed['selector']:
            links = self._announcement_links(snapshot, parsed['selector']['instrument_ids'])
            linked = {item['announcement_id'] for item in rows if item.get('announcement_id')}
            for instrument_id in parsed['selector']['instrument_ids']:
                if (instrument_id, None) in decided:
                    continue
                known = links.get(str(instrument_id), [])
                if not known:
                    output.append(_missing(instrument_id, reason, 'NO_ANNOUNCEMENT_LINK'))
                elif not set(known) & linked:
                    output.append(_missing(instrument_id, reason, 'NO_ANNOUNCEMENT_IN_RANGE'))
            return output
        covered = {self._entity_key(parsed, {'key': {'instrument_id': item.get('instrument_id'),
                                                     'index_id': item.get('index_id'),
                                                     'indicator_key': item.get('indicator_key'),
                                                     'authority': item.get('authority'),
                                                     'announcement_id': item.get('announcement_id')}})
                   for item in rows}
        for entity in self._entities(parsed):
            if entity in covered or entity in seen_entities:
                continue
            detail = 'NO_VERSION_IN_SNAPSHOT'
            if parsed['selector']['kind'] == 'instrument' and entity not in instruments['members']:
                detail = 'INSTRUMENT_NOT_IN_SNAPSHOT'
            output.append(_missing(entity, reason, detail))
        return output

    @staticmethod
    def _entities(parsed):
        selector = parsed['selector']
        if selector['kind'] == 'instrument':
            return tuple(selector['instrument_ids'])
        if selector['kind'] == 'index':
            return tuple(selector['index_ids'])
        if selector['kind'] == 'macro':
            return tuple(selector['indicator_keys'])
        if 'authority' in selector:
            return tuple(selector['authority'] + ':' + value for value in selector['announcement_ids'])
        return tuple(selector['instrument_ids'])

    def _announcement_links(self, snapshot, instrument_ids):
        """Explicit snapshot-internal instrument relation.

        A manifest may carry the optional ``announcement.instrument_links`` extension.
        A literal governance-1.4 manifest omits it; the relation is then rebuilt from
        member facts whose payload names the announcement (share_capital /
        corporate_action) and whose key carries the instrument. Never from a code guess.
        """
        entry = snapshot.entry('announcement')
        if 'instrument_links' in entry:
            return entry['instrument_links']
        wanted = set(instrument_ids)
        derived = {}
        for dataset in ('share_capital', 'corporate_action'):
            if dataset not in snapshot.document['datasets']:
                continue
            for row in self._member_facts(snapshot, dataset).values():
                if row['key'].get('instrument_id') not in wanted:
                    continue
                announcement_id = row['payload'].get('announcement_id')
                if isinstance(announcement_id, str) and announcement_id:
                    derived.setdefault(str(row['key']['instrument_id']), set()).add(announcement_id)
        return {key: sorted(value) for key, value in derived.items()}

    # --------------------------------------------------------------- adjustment
    def _adjusted(self, snapshot, parsed, row, base):
        instrument_id = row['key'].get('instrument_id')
        factor_version = self._factor_version(snapshot, parsed, instrument_id, base)
        # R06: reuse the existing price verification instead of trusting the derived row.
        # ``PriceService.adjusted`` re-derives from the pinned factor set and raw fact and
        # compares; it never derives or writes, and it fails on tampered inputs.
        try:
            verified = self._prices().adjusted(row['data_version'], factor_version)
        except WriterError as error:
            raise DataAccessError('SNAPSHOT_DEPENDENCY_MISSING', 409,
                                  'ADJUSTMENT_INTEGRITY:' + str(error)) from None
        if verified['instrument_id'] != instrument_id or verified['base_date'] != base.isoformat():
            raise DataAccessError('SNAPSHOT_DEPENDENCY_MISSING', 409, 'ADJUSTMENT_BASE_MISMATCH')
        values = {'adj_factor_version': factor_version}
        for name in ('open', 'high', 'low', 'close'):
            values['adj_' + name] = verified['adj_' + name]
        return values

    def _prices(self):
        if self._price_service is None:
            from app.services.governance_prices import PriceService
            from app.services.governance_writer import FactWriter
            self._price_service = PriceService(FactWriter(self.engine, contracts=[], clock=self.clock))
        return self._price_service

    def _factor_version(self, snapshot, parsed, instrument_id, base):
        listed = list(snapshot.adjustment_sets)
        if not listed:
            raise DataAccessError('SNAPSHOT_DEPENDENCY_MISSING', 409, 'ADJUSTMENT_FACTOR_SET_MISSING')
        with self.engine.connect() as db:
            rows = db.execute(select(s.adjustment_sets)
                              .where(s.adjustment_sets.c.adj_factor_version.in_(listed))).mappings().all()
        # 4.1: the factor set may be pinned earlier than this read (as_of <= request as_of)
        # but never later: a set frozen after the cutoff knows events we must not use.
        matches = [row for row in rows
                   if row['instrument_id'] == instrument_id and row['base_date'] == base
                   and row['knowledge_mode'] == parsed['mode'] and row['as_of'] <= parsed['cutoff']]
        if not matches:
            raise DataAccessError('SNAPSHOT_DEPENDENCY_MISSING', 409, 'ADJUSTMENT_FACTOR_SET_MISMATCH')
        newest = max(row['as_of'] for row in matches)
        chosen = [row for row in matches if row['as_of'] == newest]
        if len(chosen) != 1:
            raise DataAccessError('SNAPSHOT_DEPENDENCY_MISSING', 409, 'ADJUSTMENT_FACTOR_SET_AMBIGUOUS')
        return chosen[0]['adj_factor_version']


    def _profile(self, parsed, snapshot):
        profile_id = parsed['profile_id']
        if not isinstance(profile_id, str):
            _reject('INVALID_ARGUMENT', 'PROFILE_UNREGISTERED')
        try:
            profile = get_profile(profile_id)
        except (ValueError, TypeError, KeyError):
            _reject('INVALID_ARGUMENT', 'PROFILE_UNREGISTERED')
        if not profile_id.startswith(parsed['dataset'] + '/'):
            _reject('INVALID_ARGUMENT', 'PROFILE_DATASET_MISMATCH')
        entry = snapshot.entry(parsed['dataset'])
        bound = entry['quality_profile_versions'].get(profile_id)
        if bound is None:
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'PROFILE_NOT_BOUND')
        if bound != profile.version:
            _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'PROFILE_VERSION_MISMATCH')
        required = set(parsed['required_fields'])
        if not set(profile.required_fields) <= required:
            _reject('INVALID_ARGUMENT', 'REQUIRED_FIELDS_INCOMPLETE')
        if required - set(profile.required_fields + profile.optional_fields):
            _reject('INVALID_ARGUMENT', 'REQUIRED_FIELDS_UNKNOWN')
        return profile

    def _instruments(self, parsed, snapshot, registered):
        if parsed['selector']['kind'] != 'instrument':
            return {'ids': (), 'members': frozenset(), 'exchanges': {}}
        document = MasterDataService(self.engine).instrument_snapshot(snapshot.instrument_version)
        members = frozenset(row['instrument_id'] for row in document['members'])
        return {'ids': tuple(registered), 'members': members, 'exchanges': registered}

    def _registered(self, parsed):
        """Registry membership only: no snapshot is trusted before this check passes."""
        selector = parsed['selector']
        if selector['kind'] == 'index':
            # 14.12.3: an unknown index id is a request error, not "registered but absent".
            for index_id in selector['index_ids']:
                if index_id not in self._index_registry():
                    _reject('INVALID_ARGUMENT', 'INDEX_NOT_REGISTERED')
            return {}
        if selector['kind'] != 'instrument':
            return {}
        master = MasterDataService(self.engine)
        exchanges = {}
        for instrument_id in selector['instrument_ids']:
            try:
                exchanges[instrument_id] = master.identity(instrument_id)['exchange']
            except MasterDataError:
                _reject('INVALID_ARGUMENT', 'INSTRUMENT_NOT_REGISTERED')
        return exchanges

    def _index_registry(self):
        """Registered index ids, verified through the index master's own identity check."""
        cached = self._cache.get('index-registry')
        if cached is not None:
            return cached
        from sqlalchemy import inspect as schema_inspect
        from app.db import governance_index_schema as x
        if x.indices.name not in set(schema_inspect(self.engine).get_table_names()):
            self._cache['index-registry'] = frozenset()
            return self._cache['index-registry']
        with self.engine.connect() as db:
            rows = db.execute(select(x.indices.c.index_version, x.indices.c.index_id)).mappings().all()
        from app.services.governance_index import IndexService
        from app.services.governance_writer import FactWriter
        service = IndexService(FactWriter(self.engine, contracts=[], clock=self.clock))
        known = set()
        for row in rows:
            try:
                document = service.index(row['index_version'])
            except WriterError:
                _conflict('SNAPSHOT_DEPENDENCY_MISSING', 'INDEX_MASTER_INVALID:' + row['index_version'])
            known.add(document['index_id'])
        self._cache['index-registry'] = frozenset(known)
        return self._cache['index-registry']

    def _base_date(self, parsed, snapshot, exchanges):
        if parsed['price_basis'] != 'adj':
            return None
        wanted = set()
        for exchange in set(exchanges.values()):
            wanted.add(snapshot.base_date(parsed['aware'], exchange))
        if len(wanted) != 1:
            _reject('INVALID_ARGUMENT', 'BASE_DATE_AMBIGUOUS')
        base = wanted.pop()
        if parsed['base_date'] != base:
            _reject('INVALID_ARGUMENT', 'BASE_DATE_MISMATCH')
        return base
