"""Frozen snapshot manifest format (governance-1.4) and document-level validation.

Pure format layer: no database, no network, no publication. M08.2/3 reuse these
functions to build and publish a snapshot; M08.1 only reads a published one.
"""
from datetime import date, datetime, timezone
import json
import re

from app.services.governance_fact_contracts import canonical, digest, hash_object, instant


SCHEMA_VERSION = 'governance-1.4'
SNAPSHOT_DOMAIN = 'snapshot-v1'
DATASET_DOMAIN = 'snapshot-dataset-v1'
HASH = re.compile('[0-9a-f]{64}')
DATASETS = ('daily_bars', 'valuation', 'financial', 'capital_flow', 'lhb', 'hot_rank',
            'etf', 'index', 'macro', 'announcement', 'share_capital', 'corporate_action')
DATASET_KEYS = frozenset({'dataset_version', 'member_count', 'member_hash', 'acceptance_history_hash',
                          'contract_versions', 'definition_versions', 'source_policy_versions',
                          'quality_profile_versions'})
DEPENDENCY_KEYS = frozenset({'instrument_version', 'calendar_versions', 'session_closes', 'adjustment_sets'})
CLOSE_KEYS = frozenset({'calendar_version', 'closes', 'evidence_sha256'})
INDEX_CALENDAR = 'INDEX'
# 14.4.2: every dataset plus the shared dependency bundle names its artifact path
# and SHA-256. This stays an optional top-level extension so the M08.1 fixture
# manifests (format-identical, artifact-free) remain valid and loadable.
ARTIFACT_KEYS = frozenset({'path', 'sha256'})
ARTIFACT_NAMES = frozenset(set(DATASETS) | {'dependencies'})


class SnapshotManifestError(ValueError):
    """Format/integrity error inside a manifest document (not yet a read-contract error)."""

    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        super().__init__(code + (':' + detail if detail else ''))


def is_hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _hash(value, name):
    if not is_hash(value):
        raise SnapshotManifestError('MANIFEST_INVALID', name)
    return value


def _hash_list(values, name):
    if type(values) is not list:
        raise SnapshotManifestError('MANIFEST_INVALID', name)
    for value in values:
        _hash(value, name)
    if values != sorted(set(values)):
        raise SnapshotManifestError('MANIFEST_INVALID', name + ':UNSORTED_OR_DUPLICATE')
    return list(values)


def _artifacts(value, datasets):
    """Artifact index: one or more files per dataset and for the dependency bundle.

    Paths are relative to the snapshot root and canonical (POSIX, no traversal),
    so a manifest is relocatable and a reader can never be pointed at an
    arbitrary absolute file. Ordering is fixed to make the bytes deterministic.
    """
    if type(value) is not dict or set(value) != set(datasets) | {'dependencies'}:
        raise SnapshotManifestError('MANIFEST_INVALID', 'artifacts')
    for name, items in value.items():
        if type(items) is not list or not items:
            raise SnapshotManifestError('MANIFEST_INVALID', 'artifacts:' + name)
        paths = []
        for item in items:
            if type(item) is not dict or set(item) != ARTIFACT_KEYS:
                raise SnapshotManifestError('MANIFEST_INVALID', 'artifacts:' + name + ':KEYS')
            path = item['path']
            if not isinstance(path, str) or not path or len(path) > 260 or path.startswith(('/', '\\')) \
                    or '\\' in path or '..' in path.split('/'):
                raise SnapshotManifestError('MANIFEST_INVALID', 'artifacts:' + name + ':path')
            _hash(item['sha256'], 'artifacts:' + name + ':sha256')
            paths.append(path)
        if paths != sorted(set(paths)):
            raise SnapshotManifestError('MANIFEST_INVALID', 'artifacts:' + name + ':UNSORTED_OR_DUPLICATE')
    return value


def member_hash(dataset, members):
    """Deterministic identity of one dataset's member set (historical versions included)."""
    if dataset not in DATASETS:
        raise SnapshotManifestError('MANIFEST_INVALID', 'dataset')
    return hash_object(DATASET_DOMAIN, {'dataset': dataset, 'members': sorted(set(members))})


def acceptance_hash(dataset, rows):
    """Build-time evidence of acceptance intervals; never recomputed at read time."""
    if dataset not in DATASETS:
        raise SnapshotManifestError('MANIFEST_INVALID', 'dataset')
    return hash_object('snapshot-acceptance-v1', {'dataset': dataset, 'rows': sorted(rows, key=canonical)})


def frozen_acceptance(records, created_at):
    """The acceptance history a snapshot freezes, rebuilt from the live table.

    Two exclusions keep a replay stable without weakening integrity:
      * rows first accepted after ``created_at`` were not part of the snapshot;
      * ``system_to`` is excluded because a later supersession legitimately closes
        an interval that was open when the snapshot was built.
    Everything a reader serves (state, quality, decision, interval start) is in the
    frozen projection, so a live provenance change is detected as a hash mismatch.
    """
    cutoff = instant(created_at)
    rows = []
    for record in records:
        start = record['system_from']
        # Stored UTC columns come back naive from both engines; an offset-less value is UTC.
        start = start if start is not None and start.tzinfo else (start.replace(tzinfo=timezone.utc) if start is not None else None)
        if start is None or instant(start) > cutoff:
            continue
        rows.append({'pointer_revision': int(record['pointer_revision']), 'data_version': record['data_version'],
                     'source_state': record['source_state'], 'quality_status': record['quality_status'],
                     'system_from': instant(start).isoformat(), 'decision_id': record['decision_id']})
    return sorted(rows, key=canonical)


def dataset_identity(*, dataset, members, acceptance_history_hash, created_at):
    """Content identity of one dataset version (R3): members + acceptance + build time."""
    if dataset not in DATASETS:
        raise SnapshotManifestError('MANIFEST_INVALID', 'dataset')
    return hash_object('dataset-version-v1', {'dataset': dataset, 'members': sorted(set(members)),
                                              'acceptance': _hash(acceptance_history_hash, 'acceptance_history_hash'),
                                              'created_at': created_at})


def manifest_hash(document):
    return digest(canonical(document).encode('utf-8'))


def snapshot_identity(document):
    return hash_object(SNAPSHOT_DOMAIN, document)


def dataset_entry(*, dataset, members, acceptance_history_hash, created_at,
                  contract_versions=None, definition_versions=(), source_policy_versions=(),
                  quality_profile_versions=None, instrument_links=None):
    if dataset not in DATASETS:
        raise SnapshotManifestError('MANIFEST_INVALID', 'dataset')
    entry = {
        'dataset_version': dataset_identity(dataset=dataset, members=members,
                                            acceptance_history_hash=acceptance_history_hash,
                                            created_at=created_at),
        'member_count': len(set(members)),
        'member_hash': member_hash(dataset, members),
        'acceptance_history_hash': _hash(acceptance_history_hash, 'acceptance_history_hash'),
        'contract_versions': dict(contract_versions or {}),
        'definition_versions': sorted(set(definition_versions)),
        'source_policy_versions': sorted(set(source_policy_versions)),
        'quality_profile_versions': dict(quality_profile_versions or {}),
    }
    if dataset == 'announcement':
        # Explicit snapshot-internal relation only: announcement keys carry no instrument.
        entry['instrument_links'] = {str(k): sorted(v) for k, v in (instrument_links or {}).items()}
    return entry


def manifest_document(*, created_at, datasets, dependencies):
    """Assemble and validate; ``datasets`` maps dataset name to ``dataset_entry`` output."""
    document = {'schema_version': SCHEMA_VERSION, 'created_at': created_at,
                'datasets': dict(datasets), 'dependencies': dict(dependencies)}
    return validate_document(document)


BASE_DOCUMENT_KEYS = frozenset({'schema_version', 'created_at', 'datasets', 'dependencies'})


def validate_document(document):
    """Strict structural validation. Unknown or missing keys are rejected, never defaulted."""
    if type(document) is not dict or set(document) - (BASE_DOCUMENT_KEYS | {'artifacts'}) \
            or not BASE_DOCUMENT_KEYS <= set(document):
        raise SnapshotManifestError('MANIFEST_INVALID', 'top_level')
    if document['schema_version'] != SCHEMA_VERSION:
        raise SnapshotManifestError('MANIFEST_INVALID', 'schema_version')
    created_at = document['created_at']
    if not isinstance(created_at, str) or not created_at.endswith('Z'):
        raise SnapshotManifestError('MANIFEST_INVALID', 'created_at')
    try:
        if datetime.fromisoformat(created_at.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise SnapshotManifestError('MANIFEST_INVALID', 'created_at') from None
    datasets = document['datasets']
    if type(datasets) is not dict or not datasets or set(datasets) - set(DATASETS):
        raise SnapshotManifestError('MANIFEST_INVALID', 'datasets')
    for name, entry in datasets.items():
        # The governance-1.4 key set is exact for every dataset. `instrument_links` is
        # the one optional, announcement-only extension: a literal-spec manifest omits
        # it and the reader rebuilds the relation from member facts (M08.1 8.1).
        required = set(DATASET_KEYS)
        allowed = required | ({'instrument_links'} if name == 'announcement' else set())
        if type(entry) is not dict or not required <= set(entry) or set(entry) - allowed:
            raise SnapshotManifestError('MANIFEST_INVALID', name + ':KEYS')
        _hash(entry['dataset_version'], name + ':dataset_version')
        if type(entry['member_count']) is not int or entry['member_count'] < 0:
            raise SnapshotManifestError('MANIFEST_INVALID', name + ':member_count')
        _hash(entry['member_hash'], name + ':member_hash')
        _hash(entry['acceptance_history_hash'], name + ':acceptance_history_hash')
        contracts = entry['contract_versions']
        if type(contracts) is not dict:
            raise SnapshotManifestError('MANIFEST_INVALID', name + ':contract_versions')
        # 14.4: a published dataset must bind the unit contract and definition identity of
        # the members it certifies; an empty binding cannot cover a non-empty member set.
        if entry['member_count'] and not contracts:
            raise SnapshotManifestError('MANIFEST_INVALID', name + ':contract_versions:EMPTY')
        for version, profile in contracts.items():
            _hash(version, name + ':contract_version')
            if not isinstance(profile, str) or not profile.startswith(name + '/'):
                raise SnapshotManifestError('MANIFEST_INVALID', name + ':contract_versions')
        _hash_list(entry['definition_versions'], name + ':definition_versions')
        if entry['member_count'] and not entry['definition_versions']:
            raise SnapshotManifestError('MANIFEST_INVALID', name + ':definition_versions:EMPTY')
        _hash_list(entry['source_policy_versions'], name + ':source_policy_versions')
        profiles = entry['quality_profile_versions']
        if type(profiles) is not dict:
            raise SnapshotManifestError('MANIFEST_INVALID', name + ':quality_profile_versions')
        for profile, version in profiles.items():
            if not isinstance(profile, str) or not profile.startswith(name + '/'):
                raise SnapshotManifestError('MANIFEST_INVALID', name + ':quality_profile_versions')
            _hash(version, name + ':profile_version')
        if name == 'announcement' and 'instrument_links' in entry:
            links = entry['instrument_links']
            if type(links) is not dict:
                raise SnapshotManifestError('MANIFEST_INVALID', 'announcement:instrument_links')
            for key, value in links.items():
                if not str(key).isdigit() or int(key) <= 0 or type(value) is not list:
                    raise SnapshotManifestError('MANIFEST_INVALID', 'announcement:instrument_links')
                if any(not isinstance(item, str) or not item for item in value) or value != sorted(set(value)):
                    raise SnapshotManifestError('MANIFEST_INVALID', 'announcement:instrument_links')
    dependencies = document['dependencies']
    if type(dependencies) is not dict or set(dependencies) != DEPENDENCY_KEYS:
        raise SnapshotManifestError('MANIFEST_INVALID', 'dependencies')
    _hash(dependencies['instrument_version'], 'instrument_version')
    calendars = _hash_list(dependencies['calendar_versions'], 'calendar_versions')
    if not calendars:
        raise SnapshotManifestError('MANIFEST_INVALID', 'calendar_versions:EMPTY')
    closes = dependencies['session_closes']
    if type(closes) is not dict or set(closes) != CLOSE_KEYS:
        raise SnapshotManifestError('MANIFEST_INVALID', 'session_closes')
    if closes['calendar_version'] not in calendars:
        raise SnapshotManifestError('MANIFEST_INVALID', 'session_closes:calendar_version')
    table = closes['closes']
    if type(table) is not dict or not table:
        raise SnapshotManifestError('MANIFEST_INVALID', 'session_closes:closes')
    for exchange, days in table.items():
        if not isinstance(exchange, str) or not exchange or type(days) is not dict or not days:
            raise SnapshotManifestError('MANIFEST_INVALID', 'session_closes:exchange')
        for on, close in days.items():
            if not isinstance(close, str):
                raise SnapshotManifestError('MANIFEST_INVALID', 'session_closes:day')
            try:
                if local_date(close).isoformat() != on or date.fromisoformat(on).isoformat() != on:
                    raise SnapshotManifestError('MANIFEST_INVALID', 'session_closes:day_mismatch')
            except ValueError:
                raise SnapshotManifestError('MANIFEST_INVALID', 'session_closes:day') from None
    if closes['evidence_sha256'] != digest(canonical(table).encode('utf-8')):
        raise SnapshotManifestError('MANIFEST_HASH_MISMATCH', 'session_closes:evidence')
    _hash_list(dependencies['adjustment_sets'], 'adjustment_sets')
    if 'artifacts' in document:
        _artifacts(document['artifacts'], datasets)
    return document


def load_document(text):
    """Parse stored manifest text, refusing non-canonical or unstable bytes."""
    if not isinstance(text, str):
        raise SnapshotManifestError('MANIFEST_INVALID', 'text')
    try:
        document = json.loads(text)
    except ValueError:
        raise SnapshotManifestError('MANIFEST_INVALID', 'json') from None
    if canonical(document) != text:
        raise SnapshotManifestError('MANIFEST_HASH_MISMATCH', 'canonical')
    return validate_document(document)


def local_date(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('offset required')
    return parsed.date()


def session_close(closes, as_of, exchange):
    """Latest closed session at or before ``as_of`` for one calendar key, or None."""
    days = closes['closes'].get(exchange)
    if not days:
        return None
    cutoff = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
    closed = []
    for on, close in days.items():
        if datetime.fromisoformat(close.replace('Z', '+00:00')) <= cutoff:
            closed.append(date.fromisoformat(on))
    return max(closed) if closed else None
