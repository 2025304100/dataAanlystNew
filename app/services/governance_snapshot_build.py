"""M08.2: build one immutable shadow DuckDB snapshot from a frozen inventory.

Contract (main plan 14.4.2):
  * membership is fixed **once**, from ``acceptance_history`` rows whose
    ``system_from`` is at or before the build instant; live ``fact_current`` is
    never read, so a concurrent pointer change cannot leak into a build;
  * only immutable rows (``fact_versions``, ``version_availability``,
    ``source_decisions``, ``raw_evidence``) are copied by version id;
  * master data, calendar/session closes and the pinned adjustment sets are
    copied into one dependency artifact, so the snapshot is self-contained;
  * the manifest reuses the frozen ``governance_snapshot_manifest`` format
    (no second format, no second selection algorithm) and names every
    artifact path and SHA-256;
  * a build is verified (row counts, member/acceptance hashes, fact-v1
    identity, reference closure, dependency integrity) before it is moved to
    ``<root>/snapshots/<snapshot_id>`` and is never rewritten.

Publication state (building -> validated -> published), the pointer and rollback
are M08.3. This module writes no MySQL row and no production pointer.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from app.db import governance_master_schema as m
from app.db import governance_schema as s
from app.services.data_contract_profiles import PROFILES
from app.services.governance_fact_contracts import WriterError, canonical, digest, hash_object, instant
from app.services.governance_master_data import MasterDataError, MasterDataService, version_hash
from app.services.governance_snapshot_manifest import (
    DATASETS, INDEX_CALENDAR, SnapshotManifestError, acceptance_hash, dataset_entry,
    frozen_acceptance, is_hash, load_document, manifest_document, manifest_hash, member_hash,
    snapshot_identity, validate_document,
)

ARTIFACT_DIR = 'artifacts'
MANIFEST_FILE = 'manifest.json'
MANIFEST_HASH_FILE = 'manifest.sha256'
DEPENDENCIES = 'dependencies'
#: Session close used for the deterministic closes table; R2 stores UT timestamps.
CLOSE_TIME = '15:00:00+08:00'
MEMBER_CHUNK = 500

FACT_COLUMNS = (
    ('data_version', 'VARCHAR'), ('dataset', 'VARCHAR'), ('logical_key', 'VARCHAR'),
    ('key_json', 'VARCHAR'), ('payload_json', 'VARCHAR'), ('raw_hash', 'VARCHAR'),
    ('contract_version', 'VARCHAR'), ('definition_id', 'VARCHAR'), ('source_tag', 'VARCHAR'),
    ('field_sources', 'VARCHAR'), ('event_date', 'DATE'), ('published_at', 'VARCHAR'),
    ('publication_date', 'DATE'), ('publication_precision', 'VARCHAR'), ('public_available_at', 'VARCHAR'),
    ('observed_at', 'VARCHAR'), ('valid_from', 'DATE'), ('valid_to', 'DATE'), ('revision_kind', 'VARCHAR'),
    ('supersedes_version', 'VARCHAR'), ('publication_evidence_hash', 'VARCHAR'),
    ('source_revision_token', 'VARCHAR'), ('normalized_payload_hash', 'VARCHAR'),
    ('source_revision_order', 'VARCHAR'), ('source_revision_evidence_hash', 'VARCHAR'),
)
FACT_IDENTITY_FIELDS = ('dataset', 'logical_key', 'source_tag', 'contract_version', 'definition_id',
                        'source_revision_token', 'normalized_payload_hash', 'publication_evidence_hash',
                        'supersedes_version')


class SnapshotBuildError(ValueError):
    """Build/verification failure with a stable code; it is never a read success."""

    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        super().__init__(code + (':' + detail if detail else ''))


@dataclass(frozen=True)
class BuildResult:
    snapshot_id: str
    manifest_hash: str
    created_at: str
    directory: Path
    manifest: dict
    artifacts: dict
    counts: dict
    dependencies: dict
    reused: bool = False

    def report(self):
        return {'snapshot_id': self.snapshot_id, 'manifest_hash': self.manifest_hash,
                'created_at': self.created_at, 'directory': str(self.directory),
                'datasets': self.counts, 'artifacts': self.artifacts,
                'dependencies': self.dependencies, 'state': 'validated', 'reused': self.reused}


def _utc_text(value):
    """UTC ISO text without offset, identical to ``instant()``/frozen acceptance rows."""
    if value is None:
        return None
    if isinstance(value, str):
        value = instant(value)
    if not isinstance(value, datetime):
        raise SnapshotBuildError('INVENTORY_INVALID', 'timestamp')
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _json_text(value):
    if value is None:
        return None
    return value if isinstance(value, str) else canonical(value)


def _hashes(value):
    """Every 64-hex literal inside a key/payload: a possible fact reference."""
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


def _created_at(value):
    """Snapshot build instant in the manifest's canonical ``...Z`` spelling."""
    value = instant(value) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _connect_duckdb(path, read_only=False):
    import duckdb
    return duckdb.connect(str(path), read_only=read_only)


def _insert_many(con, statement, rows):
    """Empty datasets are legal in a snapshot; DuckDB rejects empty executemany."""
    if rows:
        con.executemany(statement, rows)


class SnapshotBuilder:
    def __init__(self, engine, root, *, clock=lambda: datetime.now(timezone.utc)):
        self.engine = engine
        self.root = Path(root)
        self.clock = clock

    # ---------------------------------------------------------------- inventory
    def build(self, datasets=None):
        created_at = _created_at(self.clock())
        requested = tuple(sorted(datasets if datasets is not None else DATASETS))
        if set(requested) - set(DATASETS):
            raise SnapshotBuildError('BUILD_ARGUMENT_INVALID', 'datasets')
        inventory = self._inventory(created_at, requested)
        staging = self.root / 'staging' / uuid.uuid4().hex
        staging.mkdir(parents=True)
        try:
            artifacts = self._write_artifacts(staging / ARTIFACT_DIR, inventory)
            document = self._document(created_at, inventory, artifacts)
            text = canonical(document)
            snapshot_id = snapshot_identity(document)
            identifier = manifest_hash(document)
            (staging / MANIFEST_FILE).write_text(text, encoding='utf-8')
            (staging / MANIFEST_HASH_FILE).write_text(identifier + '\n', encoding='utf-8')
            final = self.root / 'snapshots' / snapshot_id
            counts = {name: len(item['members']) for name, item in inventory['datasets'].items()}
            if final.exists():
                shutil.rmtree(staging)
                verify_snapshot(self.root, snapshot_id)
                return BuildResult(snapshot_id, identifier, created_at, final, document, artifacts, counts,
                                   inventory['summary'], reused=True)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            verify_snapshot(self.root, snapshot_id)
        except Exception:
            shutil.rmtree(final, ignore_errors=True)
            raise
        return BuildResult(snapshot_id, identifier, created_at, final, document, artifacts, counts,
                           inventory['summary'])

    def _inventory(self, created_at, requested):
        cutoff = instant(created_at)
        with self.engine.connect() as db:
            # One statement freezes the member list. No fact_current read exists here:
            # a concurrent pointer change can only affect rows already copied.
            accepted = db.execute(select(s.acceptance_history)
                                  .where(s.acceptance_history.c.system_from <= cutoff)).mappings().all()
            groups = {}
            for row in accepted:
                groups.setdefault(row['dataset'], []).append(dict(row))
            datasets = {}
            for name in requested:
                records = groups.get(name, [])
                members = sorted({row['data_version'] for row in records})
                frozen = frozen_acceptance(records, created_at)
                facts = self._facts(db, name, members)
                availability = self._availability(db, members)
                decisions = self._decisions(db, name, members, frozen, cutoff)
                evidence = self._evidence(db, facts, decisions)
                datasets[name] = {'members': members, 'facts': facts, 'availability': availability,
                                  'acceptance': frozen, 'decisions': decisions, 'evidence': evidence}
            unknown = set(groups) - set(DATASETS)
            if unknown:
                raise SnapshotBuildError('INVENTORY_INVALID', 'UNKNOWN_DATASET:' + sorted(unknown)[0])
            all_members = frozenset().union(*[set(item['members']) for item in datasets.values()]) \
                if datasets else frozenset()
            self._closure(db, datasets, all_members)
            dependencies = self._dependencies(db, cutoff, datasets)
        summary = {'source_high_water': dependencies['source_high_water'],
                   'member_total': len(all_members),
                   'coverage': {'denominator_status': 'unknown', 'coverage': None},
                   'datasets': {name: len(item['members']) for name, item in datasets.items()},
                   'accepted_rows': sum(len(item['acceptance']) for item in datasets.values())}
        return {'created_at': created_at, 'datasets': datasets, 'dependencies': dependencies, 'summary': summary}

    @staticmethod
    def _facts(db, dataset, members):
        rows = {}
        for start in range(0, len(members), MEMBER_CHUNK):
            chunk = members[start:start + MEMBER_CHUNK]
            for row in db.execute(select(s.fact_versions)
                                  .where(s.fact_versions.c.data_version.in_(chunk))).mappings():
                rows[row['data_version']] = dict(row)
        if set(rows) != set(members) or any(row['dataset'] != dataset for row in rows.values()):
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'MEMBER_FACT_MISSING:' + dataset)
        return rows

    @staticmethod
    def _availability(db, members):
        rows = {}
        for start in range(0, len(members), MEMBER_CHUNK):
            chunk = members[start:start + MEMBER_CHUNK]
            for row in db.execute(select(s.version_availability)
                                  .where(s.version_availability.c.data_version.in_(chunk))).mappings():
                rows[row['data_version']] = dict(row)
        return rows

    @staticmethod
    def _decisions(db, dataset, members, frozen, cutoff):
        wanted = {row['decision_id'] for row in frozen}
        rows = {}
        for start in range(0, len(members), MEMBER_CHUNK):
            chunk = members[start:start + MEMBER_CHUNK]
            for row in db.execute(select(s.source_decisions)
                                  .where(s.source_decisions.c.candidate_version.in_(chunk))).mappings():
                if row['dataset'] != dataset or row['decided_at'] > cutoff:
                    continue
                rows[row['decision_id']] = dict(row)
        missing = wanted - set(rows)
        if missing:
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'ACCEPTANCE_DECISION_MISSING:' + sorted(missing)[0])
        return rows

    @staticmethod
    def _evidence(db, facts, decisions):
        wanted = set()
        for row in facts.values():
            for name in ('raw_hash', 'publication_evidence_hash'):
                if row[name]:
                    wanted.add(row[name])
        for row in decisions.values():
            wanted.add(row['evidence_hash'])
        rows = {}
        hashes = sorted(wanted)
        for start in range(0, len(hashes), MEMBER_CHUNK):
            chunk = hashes[start:start + MEMBER_CHUNK]
            for row in db.execute(select(s.raw_evidence).where(s.raw_evidence.c.raw_hash.in_(chunk))).mappings():
                rows[row['raw_hash']] = dict(row)
        if set(rows) != wanted:
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'RAW_EVIDENCE_MISSING:' + sorted(wanted - set(rows))[0])
        return rows

    def _dependencies(self, db, cutoff, datasets):
        control = db.execute(select(m.control).where(m.control.c.id == 1)).mappings().first()
        instrument_version = control['instrument_head'] if control is not None else None
        if not is_hash(instrument_version):
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'INSTRUMENT_VERSION_MISSING')
        master = MasterDataService(self.engine)
        try:
            instrument_document = master.instrument_snapshot(instrument_version)
        except (MasterDataError, WriterError, ValueError):
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'INSTRUMENT_VERSION_UNAVAILABLE') from None
        calendars = db.execute(select(m.versions).where(m.versions.c.kind == 'calendar')).mappings().all()
        if not calendars:
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'CALENDAR_VERSION_MISSING')
        calendar_versions = sorted(row['version'] for row in calendars)
        for row in calendars:
            try:
                master.calendar_snapshot(row['version'])
            except (MasterDataError, WriterError, ValueError):
                raise SnapshotBuildError('DEPENDENCY_MISSING', 'CALENDAR_VERSION_UNAVAILABLE') from None
        sessions = max(calendars, key=lambda row: (_utc_text(row['observed_at']), row['version']))
        calendar_version = sessions['version']
        try:
            document = master.calendar_snapshot(calendar_version)
        except (MasterDataError, WriterError, ValueError):
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'CALENDAR_VERSION_UNAVAILABLE') from None
        exchange = document['exchange']
        first, last = date.fromisoformat(document['start']), date.fromisoformat(document['end_exclusive'])
        close_map = {}
        try:
            days = master.trading_days(calendar_version, exchange, first, last)
        except (MasterDataError, ValueError):
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'SESSION_CLOSE_CALENDAR:' + exchange) from None
        close_map[exchange] = {value.isoformat(): value.isoformat() + 'T' + CLOSE_TIME for value in days}
        close_map[INDEX_CALENDAR] = dict(close_map[exchange])
        closes = {'calendar_version': calendar_version, 'closes': close_map,
                  'evidence_sha256': digest(canonical(close_map).encode('utf-8'))}

        # Every daily bar member must be covered by this calendar; another exchange's
        # invented close table would be a false dependency, so the build refuses.
        identities = {row['instrument_id']: row for row in instrument_document['identities']}
        for fact in self._daily_facts(datasets):
            exchange_of = identities.get(fact['key'].get('instrument_id'), {}).get('exchange')
            if exchange_of != exchange:
                raise SnapshotBuildError('DEPENDENCY_MISSING', 'CALENDAR_EXCHANGE_UNSUPPORTED:' + str(exchange_of))

        sets = db.execute(select(s.adjustment_sets)
                          .where(s.adjustment_sets.c.as_of <= cutoff)).mappings().all()
        factor_rows = {}
        inputs = {}
        bars = {}
        input_evidence = set()
        referenced_versions = set()
        inspector = None
        from app.db import governance_price_schema as p
        for row in sets:
            version = row['adj_factor_version']
            factor_rows[version] = list(db.execute(select(s.adjustment_factor_rows)
                                                   .where(s.adjustment_factor_rows.c.adj_factor_version == version)
                                                   .order_by(s.adjustment_factor_rows.c.trade_date)).mappings())
            bars[version] = list(db.execute(select(s.adjusted_bars)
                                            .where(s.adjusted_bars.c.adj_factor_version == version)).mappings())
            if inspector is None:
                from sqlalchemy import inspect as schema_inspect
                inspector = set(schema_inspect(self.engine).get_table_names())
            if p.inputs.name in inspector:
                inputs[version] = db.execute(select(p.inputs)
                                             .where(p.inputs.c.adj_factor_version == version)).mappings().first()
                if inputs[version] is not None:
                    input_evidence.add(inputs[version]['evidence_hash'])
            else:
                inputs[version] = None
            manifest = {'spec': {'raw_data_versions': [], 'event_inputs': [], 'instrument_version': None}}
            if inputs[version] is not None:
                try:
                    manifest = json.loads(inputs[version]['manifest_json'])
                except ValueError:
                    raise SnapshotBuildError('DEPENDENCY_MISSING', 'FACTOR_INPUT_INVALID:' + version) from None
            spec = manifest.get('spec') if isinstance(manifest, dict) else None
            if not isinstance(spec, dict):
                raise SnapshotBuildError('DEPENDENCY_MISSING', 'FACTOR_INPUT_INVALID:' + version)
            referenced_versions |= set(spec.get('raw_data_versions') or [])
            for event in spec.get('event_inputs') or []:
                if isinstance(event, dict):
                    referenced_versions |= {event.get('data_version'), event.get('previous_raw_data_version')}
            referenced_versions.discard(None)
            if not is_hash(spec.get('instrument_version')):
                raise SnapshotBuildError('DEPENDENCY_MISSING', 'FACTOR_INSTRUMENT_VERSION_MISSING:' + version)

        members = frozenset().union(*[set(item['members']) for item in datasets.values()]) if datasets else frozenset()
        outside = sorted(str(value) for value in referenced_versions
                         if not is_hash(value) or value not in members)
        if outside:
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'FACTOR_INPUT_OUTSIDE_SNAPSHOT:' + outside[0])
        for version, rows in bars.items():
            for row in rows:
                if row['raw_data_version'] not in members:
                    raise SnapshotBuildError('DEPENDENCY_MISSING', 'ADJUSTED_RAW_OUTSIDE_SNAPSHOT')

        wanted_versions = {instrument_version, *calendar_versions}
        for row in sets:
            manifest = json.loads(inputs[row['adj_factor_version']]['manifest_json']) \
                if inputs[row['adj_factor_version']] is not None else None
            if manifest is not None:
                wanted_versions.add(manifest['spec']['instrument_version'])
        master_rows = {}
        for start in range(0, len(wanted_versions), MEMBER_CHUNK):
            chunk = sorted(wanted_versions)[start:start + MEMBER_CHUNK]
            for row in db.execute(select(m.versions).where(m.versions.c.version.in_(chunk))).mappings():
                master_rows[row['version']] = dict(row)
        if set(master_rows) != wanted_versions:
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'MASTER_VERSION_MISSING')
        raw = {}
        evidence_hashes = sorted(input_evidence)
        for start in range(0, len(evidence_hashes), MEMBER_CHUNK):
            chunk = evidence_hashes[start:start + MEMBER_CHUNK]
            for row in db.execute(select(s.raw_evidence).where(s.raw_evidence.c.raw_hash.in_(chunk))).mappings():
                raw[row['raw_hash']] = dict(row)
        if set(raw) != input_evidence:
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'FACTOR_INPUT_EVIDENCE_MISSING')
        outbox = db.execute(select(s.governance_outbox.c.created_at)
                            .order_by(s.governance_outbox.c.created_at.desc()).limit(1)).scalar_one_or_none()
        return {'instrument_version': instrument_version, 'instrument_document': instrument_document,
                'calendar_versions': calendar_versions, 'calendar_version': calendar_version,
                'calendar_document': document, 'closes': closes, 'master_rows': master_rows,
                'adjustment_sets': [dict(row) for row in sets], 'factor_rows': factor_rows,
                'adjusted_bars': bars, 'inputs': {key: (dict(value) if value is not None else None)
                                                  for key, value in inputs.items()},
                'input_evidence': raw,
                'source_high_water': {'outbox_created_at': _utc_text(outbox)}}

    @staticmethod
    def _closure(db, datasets, all_members):
        """R03 at build time: a member that points at a known version must point inside.

        The check runs against the frozen database, so a definition/contract hash that
        is not a fact version is simply not found (never a false dependency).
        """
        candidates = set()
        for item in datasets.values():
            for fact in item['facts'].values():
                for value in (json.loads(fact['key_json']), json.loads(fact['payload_json'])):
                    candidates |= _hashes(value)
        outside = candidates - set(all_members)
        if not outside:
            return
        discovered = set()
        values = sorted(outside)
        for start in range(0, len(values), MEMBER_CHUNK):
            chunk = values[start:start + MEMBER_CHUNK]
            discovered |= set(db.execute(select(s.fact_versions.c.data_version)
                                         .where(s.fact_versions.c.data_version.in_(chunk))).scalars())
        if discovered:
            raise SnapshotBuildError('REFERENCE_OUTSIDE_SNAPSHOT', sorted(discovered)[0])

    @staticmethod
    def _daily_facts(datasets):
        facts = []
        for item in datasets.get('daily_bars', {}).get('facts', {}).values():
            facts.append({'key': json.loads(item['key_json'])})
        return facts

    # ---------------------------------------------------------------- documents
    def _document(self, created_at, inventory, artifacts):
        datasets = {}
        profiles = {}
        for profile in PROFILES:
            profiles.setdefault(profile.profile_id.split('/', 1)[0], {})[profile.profile_id] = profile.version
        for name, item in inventory['datasets'].items():
            facts = item['facts']
            contracts = {}
            definitions = set()
            for row in facts.values():
                label = (name + '/core') if (name + '/core') in profiles.get(name, {}) \
                    else sorted(profiles.get(name, {}))[0]
                contracts[row['contract_version']] = label
                definitions.add(row['definition_id'])
            entry = dataset_entry(dataset=name, members=item['members'],
                                  acceptance_history_hash=acceptance_hash(name, item['acceptance']),
                                  created_at=created_at, contract_versions=contracts,
                                  definition_versions=sorted(definitions), source_policy_versions=[],
                                  quality_profile_versions=profiles.get(name, {}),
                                  instrument_links=self._instrument_links(name, inventory))
            datasets[name] = entry
        dependencies = {'instrument_version': inventory['dependencies']['instrument_version'],
                        'calendar_versions': list(inventory['dependencies']['calendar_versions']),
                        'session_closes': dict(inventory['dependencies']['closes']),
                        'adjustment_sets': sorted(row['adj_factor_version']
                                                  for row in inventory['dependencies']['adjustment_sets'])}
        document = manifest_document(created_at=created_at, datasets=datasets, dependencies=dependencies)
        document['artifacts'] = {name: [{'path': item['path'],
                                         'sha256': item['sha256']}] for name, item in artifacts.items()}
        return validate_document(document)

    @staticmethod
    def _instrument_links(name, inventory):
        """Only announcements that are members of this snapshot may be linked."""
        if name != 'announcement':
            return None
        known = {json.loads(fact['key_json']).get('announcement_id')
                 for fact in inventory['datasets'].get('announcement', {}).get('facts', {}).values()}
        links = {}
        for dataset in ('share_capital', 'corporate_action'):
            for fact in inventory['datasets'].get(dataset, {}).get('facts', {}).values():
                key = json.loads(fact['key_json'])
                payload = json.loads(fact['payload_json'])
                announcement = payload.get('announcement_id')
                if isinstance(announcement, str) and announcement in known:
                    links.setdefault(str(key['instrument_id']), set()).add(announcement)
        return {key: sorted(value) for key, value in links.items() if value}

    # ---------------------------------------------------------------- artifacts
    def _write_artifacts(self, directory, inventory):
        directory.mkdir(parents=True)
        mapping = {}
        for name, item in inventory['datasets'].items():
            path = directory / (name + '.duckdb')
            self._write_dataset(path, name, item)
            mapping[name] = {'path': ARTIFACT_DIR + '/' + name + '.duckdb',
                             'sha256': _sha(path), 'rows': len(item['members'])}
        path = directory / (DEPENDENCIES + '.duckdb')
        self._write_dependencies(path, inventory['dependencies'])
        mapping[DEPENDENCIES] = {'path': ARTIFACT_DIR + '/' + DEPENDENCIES + '.duckdb',
                                 'sha256': _sha(path), 'rows': len(inventory['dependencies']['master_rows'])}
        if set(mapping) != set(inventory['datasets']) | {DEPENDENCIES}:
            raise SnapshotBuildError('BUILD_INVENTORY_INVALID', 'artifacts')
        return mapping

    @staticmethod
    def _write_dataset(path, dataset, item):
        con = _connect_duckdb(path)
        try:
            con.execute('CREATE TABLE fact_versions (' + ', '.join(name + ' ' + kind + (' NOT NULL' if name in
                        ('data_version', 'dataset', 'logical_key', 'key_json', 'payload_json', 'raw_hash',
                         'contract_version', 'definition_id', 'source_tag', 'field_sources', 'event_date',
                         'publication_precision', 'observed_at', 'valid_from', 'revision_kind',
                         'source_revision_token', 'normalized_payload_hash') else '')
                        for name, kind in FACT_COLUMNS) + ', PRIMARY KEY (data_version))')
            con.execute('CREATE TABLE version_availability (data_version VARCHAR NOT NULL, available_at VARCHAR NOT NULL, '
                        'decision_id VARCHAR NOT NULL, PRIMARY KEY (data_version))')
            con.execute('CREATE TABLE acceptance_history (pointer_revision BIGINT NOT NULL, data_version VARCHAR NOT NULL, '
                        'source_state VARCHAR NOT NULL, quality_status VARCHAR NOT NULL, system_from VARCHAR NOT NULL, '
                        'decision_id VARCHAR NOT NULL, PRIMARY KEY (pointer_revision, data_version))')
            con.execute('CREATE TABLE source_decisions (decision_id VARCHAR NOT NULL, dataset VARCHAR NOT NULL, '
                        'logical_key VARCHAR NOT NULL, candidate_version VARCHAR NOT NULL, previous_version VARCHAR, '
                        'event VARCHAR NOT NULL, from_state VARCHAR NOT NULL, to_state VARCHAR NOT NULL, actor VARCHAR NOT NULL, '
                        'evidence_hash VARCHAR NOT NULL, decided_at VARCHAR NOT NULL, '
                        'expected_pointer_revision BIGINT NOT NULL, result VARCHAR NOT NULL, PRIMARY KEY (decision_id))')
            con.execute('CREATE TABLE raw_evidence (raw_hash VARCHAR NOT NULL, raw_bytes BLOB NOT NULL, '
                        'source_uri VARCHAR NOT NULL, observed_at VARCHAR NOT NULL, PRIMARY KEY (raw_hash))')
            fact_rows = [tuple(_value(row, name) for name, _kind in FACT_COLUMNS)
                         for row in item['facts'].values()]
            _insert_many(con, 'INSERT INTO fact_versions VALUES (' +
                         ', '.join('?' for _ in FACT_COLUMNS) + ')', fact_rows)
            availability = [(row['data_version'], _utc_text(row['available_at']), row['decision_id'])
                            for row in item['availability'].values()]
            _insert_many(con, 'INSERT INTO version_availability VALUES (?, ?, ?)', availability)
            _insert_many(con, 'INSERT INTO acceptance_history VALUES (?, ?, ?, ?, ?, ?)',
                         [(row['pointer_revision'], row['data_version'], row['source_state'],
                           row['quality_status'], row['system_from'], row['decision_id'])
                          for row in item['acceptance']])
            _insert_many(con, 'INSERT INTO source_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         [(row['decision_id'], row['dataset'], row['logical_key'], row['candidate_version'],
                           row['previous_version'], row['event'], row['from_state'], row['to_state'],
                           row['actor'], row['evidence_hash'], _utc_text(row['decided_at']),
                           row['expected_pointer_revision'], row['result'])
                          for row in item['decisions'].values()])
            _insert_many(con, 'INSERT INTO raw_evidence VALUES (?, ?, ?, ?)',
                         [(row['raw_hash'], bytes(row['raw_bytes']), row['source_uri'], _utc_text(row['observed_at']))
                          for row in item['evidence'].values()])
        finally:
            con.close()

    @staticmethod
    def _write_dependencies(path, dependencies):
        con = _connect_duckdb(path)
        try:
            con.execute('CREATE TABLE master_versions (version VARCHAR NOT NULL, kind VARCHAR NOT NULL, '
                        'document_json VARCHAR NOT NULL, parent_version VARCHAR, evidence_sha256 VARCHAR NOT NULL, '
                        'evidence BLOB NOT NULL, observed_at VARCHAR NOT NULL, PRIMARY KEY (version))')
            con.execute('CREATE TABLE session_closes (exchange VARCHAR NOT NULL, trade_date DATE NOT NULL, '
                        'close_at VARCHAR NOT NULL, PRIMARY KEY (exchange, trade_date))')
            con.execute('CREATE TABLE adjustment_sets (adj_factor_version VARCHAR NOT NULL, instrument_id BIGINT NOT NULL, '
                        'base_date DATE NOT NULL, as_of VARCHAR NOT NULL, knowledge_mode VARCHAR NOT NULL, '
                        'algorithm_version VARCHAR NOT NULL, event_version_ids VARCHAR NOT NULL, '
                        'factor_rows_hash VARCHAR NOT NULL, PRIMARY KEY (adj_factor_version))')
            con.execute('CREATE TABLE adjustment_factor_rows (adj_factor_version VARCHAR NOT NULL, '
                        'trade_date DATE NOT NULL, factor DECIMAL(28,16) NOT NULL, '
                        'PRIMARY KEY (adj_factor_version, trade_date))')
            con.execute('CREATE TABLE adjusted_bars (raw_data_version VARCHAR NOT NULL, '
                        'adj_factor_version VARCHAR NOT NULL, instrument_id BIGINT NOT NULL, trade_date DATE NOT NULL, '
                        'base_date DATE NOT NULL, adj_open DECIMAL(24,8) NOT NULL, adj_high DECIMAL(24,8) NOT NULL, '
                        'adj_low DECIMAL(24,8) NOT NULL, adj_close DECIMAL(24,8) NOT NULL, '
                        'PRIMARY KEY (raw_data_version, adj_factor_version))')
            con.execute('CREATE TABLE adjustment_set_inputs (adj_factor_version VARCHAR NOT NULL, '
                        'manifest_json VARCHAR NOT NULL, evidence_hash VARCHAR NOT NULL, '
                        'PRIMARY KEY (adj_factor_version))')
            _insert_many(con, 'INSERT INTO master_versions VALUES (?, ?, ?, ?, ?, ?, ?)',
                         [(row['version'], row['kind'], row['document_json'], row['parent_version'],
                           digest(bytes(row['evidence'])), bytes(row['evidence']), _utc_text(row['observed_at']))
                          for row in sorted(dependencies['master_rows'].values(), key=lambda item: item['version'])])
            closes = dependencies['closes']['closes']
            _insert_many(con, 'INSERT INTO session_closes VALUES (?, ?, ?)',
                         [(exchange, date.fromisoformat(on), close)
                          for exchange, days in closes.items() for on, close in sorted(days.items())])
            _insert_many(con, 'INSERT INTO adjustment_sets VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         [(row['adj_factor_version'], row['instrument_id'], row['base_date'],
                           _utc_text(row['as_of']), row['knowledge_mode'], row['algorithm_version'],
                           _json_text(row['event_version_ids']), row['factor_rows_hash'])
                          for row in sorted(dependencies['adjustment_sets'], key=lambda item: item['adj_factor_version'])])
            rows = [[row['adj_factor_version'], row['trade_date'], row['factor']]
                    for key in sorted(dependencies['factor_rows']) for row in dependencies['factor_rows'][key]]
            _insert_many(con, 'INSERT INTO adjustment_factor_rows VALUES (?, ?, ?)', rows)
            bars = [[row['raw_data_version'], row['adj_factor_version'], row['instrument_id'], row['trade_date'],
                     row['base_date'], row['adj_open'], row['adj_high'], row['adj_low'], row['adj_close']]
                    for key in sorted(dependencies['adjusted_bars']) for row in dependencies['adjusted_bars'][key]]
            _insert_many(con, 'INSERT INTO adjusted_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', bars)
            _insert_many(con, 'INSERT INTO adjustment_set_inputs VALUES (?, ?, ?)',
                         [(key, row['manifest_json'], row['evidence_hash'])
                          for key, row in sorted(dependencies['inputs'].items()) if row is not None])
        finally:
            con.close()


def _value(row, name):
    if name in {'event_date', 'publication_date', 'valid_from', 'valid_to'}:
        return row[name]
    if name == 'field_sources' or name == 'source_revision_order':
        return _json_text(row[name])
    if name == 'observed_at' or name == 'published_at' or name == 'public_available_at':
        return _utc_text(row[name])
    return row[name]


# ---------------------------------------------------------------- verification
def verify_snapshot(root, snapshot_id):
    root = Path(root)
    identifier = snapshot_id
    directory = root / 'snapshots' / identifier
    manifest_path = directory / MANIFEST_FILE
    if not manifest_path.is_file():
        raise SnapshotBuildError('ARTIFACT_MISSING', MANIFEST_FILE)
    text = manifest_path.read_text(encoding='utf-8')
    try:
        document = load_document(text)
    except SnapshotManifestError as error:
        raise SnapshotBuildError('MANIFEST_INVALID', error.code + (':' + error.detail if error.detail else '')) from None
    if snapshot_identity(document) != identifier:
        raise SnapshotBuildError('SNAPSHOT_ID_MISMATCH', identifier)
    if 'artifacts' not in document:
        raise SnapshotBuildError('ARTIFACT_MISSING', 'MANIFEST_ARTIFACTS')
    artifacts = document['artifacts']
    if set(artifacts) != set(document['datasets']) | {DEPENDENCIES}:
        raise SnapshotBuildError('MANIFEST_INVALID', 'artifacts')
    counts = {}
    for name, items in artifacts.items():
        for item in items:
            path = directory / item['path']
            if not path.is_file():
                raise SnapshotBuildError('ARTIFACT_MISSING', name + ':' + item['path'])
            if _sha(path) != item['sha256']:
                raise SnapshotBuildError('ARTIFACT_HASH_MISMATCH', name + ':' + item['path'])
    for name in document['datasets']:
        _members, counts[name] = _verify_dataset(directory / artifacts[name][0]['path'], name, document)
    dependencies = _verify_dependencies(directory / artifacts[DEPENDENCIES][0]['path'], document)
    return {'snapshot_id': identifier, 'manifest_hash': manifest_hash(document),
            'created_at': document['created_at'], 'datasets': counts, 'dependencies': dependencies,
            'status': 'pass'}


def _verify_dataset(path, dataset, document):
    entry = document['datasets'][dataset]
    con = _connect_duckdb(path, read_only=True)
    try:
        rows = con.execute('SELECT ' + ', '.join(name for name, _ in FACT_COLUMNS) +
                           ' FROM fact_versions ORDER BY data_version').fetchall()
        labels = [name for name, _ in FACT_COLUMNS]
        facts = [dict(zip(labels, row)) for row in rows]
        versions = [row['data_version'] for row in facts]
        if len(facts) != entry['member_count'] or member_hash(dataset, versions) != entry['member_hash']:
            raise SnapshotBuildError('MEMBER_HASH_MISMATCH', dataset)
        accepted = con.execute('SELECT pointer_revision, data_version, source_state, quality_status, '
                               'system_from, decision_id FROM acceptance_history').fetchall()
        frozen = [{'pointer_revision': row[0], 'data_version': row[1], 'source_state': row[2],
                   'quality_status': row[3], 'system_from': row[4], 'decision_id': row[5]} for row in accepted]
        if acceptance_hash(dataset, frozen) != entry['acceptance_history_hash']:
            raise SnapshotBuildError('ACCEPTANCE_HASH_MISMATCH', dataset)
        for fact in facts:
            if fact['dataset'] != dataset or not is_hash(fact['data_version']):
                raise SnapshotBuildError('FACT_INTEGRITY', dataset)
            if digest(fact['key_json'].encode('utf-8')) != fact['logical_key']:
                raise SnapshotBuildError('FACT_INTEGRITY', fact['data_version'])
            if digest(fact['payload_json'].encode('utf-8')) != fact['normalized_payload_hash']:
                raise SnapshotBuildError('FACT_INTEGRITY', fact['data_version'])
            identity = {name: fact[name] for name in FACT_IDENTITY_FIELDS}
            if hash_object('fact-v1', identity) != fact['data_version']:
                raise SnapshotBuildError('FACT_INTEGRITY', fact['data_version'])
            if fact['supersedes_version'] and fact['supersedes_version'] not in versions:
                raise SnapshotBuildError('REFERENCE_OUTSIDE_SNAPSHOT', fact['supersedes_version'])
        evidence = {row[0]: row[1] for row in con.execute('SELECT raw_hash, raw_bytes FROM raw_evidence').fetchall()}
        for key, value in evidence.items():
            if digest(bytes(value)) != key:
                raise SnapshotBuildError('FACT_INTEGRITY', 'RAW_EVIDENCE:' + key)
        decisions = con.execute('SELECT decision_id, evidence_hash FROM source_decisions').fetchall()
        for decision_id, evidence_hash in decisions:
            if evidence_hash not in evidence:
                raise SnapshotBuildError('DEPENDENCY_MISSING', 'DECISION_EVIDENCE_MISSING:' + decision_id)
        for fact in facts:
            for name in ('raw_hash', 'publication_evidence_hash'):
                if fact[name] and fact[name] not in evidence:
                    raise SnapshotBuildError('DEPENDENCY_MISSING', 'RAW_EVIDENCE_MISSING:' + fact[name])
        return frozenset(versions), {'members': len(versions), 'rows': len(facts),
                                     'acceptance_rows': len(frozen)}
    finally:
        con.close()


def _verify_dependencies(path, document):
    con = _connect_duckdb(path, read_only=True)
    try:
        masters = con.execute('SELECT version, kind, document_json, evidence_sha256, evidence FROM master_versions').fetchall()
        master_versions = set()
        for version, kind, text, evidence_sha256, evidence in masters:
            try:
                parsed = json.loads(text)
                if canonical(parsed) != text or version_hash(kind, parsed) != version:
                    raise ValueError()
            except (ValueError, TypeError, WriterError):
                raise SnapshotBuildError('DEPENDENCY_INTEGRITY', version) from None
            if digest(bytes(evidence)) != evidence_sha256 or parsed.get('evidence_hash') != evidence_sha256:
                raise SnapshotBuildError('DEPENDENCY_INTEGRITY', version)
            master_versions.add(version)
        required = {document['dependencies']['instrument_version']} | set(document['dependencies']['calendar_versions'])
        if required - master_versions:
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'MASTER_VERSION_MISSING')
        closes = con.execute('SELECT exchange, trade_date, close_at FROM session_closes').fetchall()
        table = {exchange: {} for exchange, _day, _close in closes}
        for exchange, trade_date, close_at in closes:
            table.setdefault(exchange, {})[trade_date.isoformat()] = close_at
        if table != document['dependencies']['session_closes']['closes']:
            raise SnapshotBuildError('DEPENDENCY_INTEGRITY', 'SESSION_CLOSES')
        sets = {row[0] for row in con.execute('SELECT adj_factor_version FROM adjustment_sets').fetchall()}
        if set(document['dependencies']['adjustment_sets']) - sets:
            raise SnapshotBuildError('DEPENDENCY_MISSING', 'ADJUSTMENT_SET_MISSING')
        for version in sets:
            rows = con.execute('SELECT trade_date, factor FROM adjustment_factor_rows '
                               'WHERE adj_factor_version = ? ORDER BY trade_date', [version]).fetchall()
            stored = [{'trade_date': row[0].isoformat(), 'factor': format(row[1], '.16f')} for row in rows]
            expected = con.execute('SELECT factor_rows_hash FROM adjustment_sets '
                                   'WHERE adj_factor_version = ?', [version]).fetchone()[0]
            if hash_object('factor-rows-v1', stored) != expected:
                raise SnapshotBuildError('DEPENDENCY_INTEGRITY', 'FACTOR_ROWS:' + version)
        return {'master_versions': len(master_versions), 'session_closes': sum(len(value) for value in table.values()),
                'adjustment_sets': len(sets)}
    finally:
        con.close()
