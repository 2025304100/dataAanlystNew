"""M08.2: frozen member/dependency inventory into an immutable shadow DuckDB snapshot.

Every assertion is about the real build contract (14.4.2), not a mock:
membership really comes from ``acceptance_history``, payloads are the bytes the
single FactWriter stored, and corruption/missing-dependency paths are exercised
by changing the produced files or the candidate database.
"""
from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import delete, select

from app.db import governance_schema as s
from app.services.governance_snapshot_build import SnapshotBuildError, SnapshotBuilder, verify_snapshot
from app.services.governance_snapshot_manifest import (
    SnapshotManifestError, acceptance_hash, canonical, dataset_identity, frozen_acceptance, load_document,
    manifest_hash, snapshot_identity, validate_document,
)
from tests.data_governance.test_as_of_and_snapshots import lab
from tests.test_governance_master_data import annual_page
from tests.test_governance_schema import candidate

pytestmark = pytest.mark.whitebox


def build(lab, root, datasets=None):
    return SnapshotBuilder(lab.engine, root, clock=lab.clock).build(datasets=datasets)


def artifact_file(result, name):
    entry = result.manifest['artifacts'][name][0]
    return result.directory / entry['path']


def rows(path, table):
    import duckdb
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute('SELECT * FROM ' + table).fetchall()
    finally:
        con.close()


def test_build_produces_a_validated_self_contained_snapshot(lab, tmp_path):
    """ST-M08.2: manifest/artifact identity is derived, hashed and verified."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    lab.at(datetime(2026, 5, 2, 1, tzinfo=timezone.utc))
    result = build(lab, tmp_path / 'root', datasets=['financial'])

    assert result.report()['state'] == 'validated'
    assert result.snapshot_id == snapshot_identity(result.manifest)
    assert (result.directory / 'manifest.sha256').read_text(encoding='utf-8').strip() == result.manifest_hash
    text = (result.directory / 'manifest.json').read_text(encoding='utf-8')
    assert canonical(result.manifest) == text
    assert load_document(text) == result.manifest
    assert snapshot_identity(load_document(text)) == result.snapshot_id
    assert manifest_hash(load_document(text)) == result.manifest_hash

    assert set(result.manifest['artifacts']) == {'financial', 'dependencies'}
    for name, items in result.manifest['artifacts'].items():
        for item in items:
            path = result.directory / item['path']
            import hashlib
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
    entry = result.manifest['datasets']['financial']
    assert entry['member_count'] == 1
    assert dataset_identity(dataset='financial', members=[version],
                            acceptance_history_hash=entry['acceptance_history_hash'],
                            created_at=result.manifest['created_at']) == entry['dataset_version']
    verified = verify_snapshot(tmp_path / 'root', result.snapshot_id)
    assert verified['status'] == 'pass'
    assert verified['datasets']['financial'] == {'members': 1, 'rows': 1, 'acceptance_rows': 1}
    assert verified['dependencies']['master_versions'] == 2


def test_build_freezes_members_before_a_live_current_and_acceptance_change(lab, tmp_path, monkeypatch):
    """ST-M08.2: a pointer/acceptance change during the build cannot enter it.

    The mutation happens after the frozen inventory is read and before the
    artifacts are written: this is the real ``current``-drift window.
    """
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    first = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00', revision_order=1)
    original = SnapshotBuilder._inventory
    captured = {}

    def inventory_then_change(self, created_at, requested):
        inventory = original(self, created_at, requested)
        with lab.engine.connect() as db:
            captured['rows'] = [dict(row) for row in db.execute(
                select(s.acceptance_history).where(s.acceptance_history.c.dataset == 'financial')).mappings()]
        # A correction is accepted and becomes current while the build is running.
        lab.at(datetime(2026, 6, 2, 10, tzinfo=timezone.utc))
        lab.financial('2026-03-31', '80', published_at='2026-06-01T10:00:00+08:00', revision_order=2,
                      supersedes=first, acceptance_event='T09', revision=1)
        return inventory

    monkeypatch.setattr(SnapshotBuilder, '_inventory', inventory_then_change)
    result = build(lab, tmp_path / 'root', datasets=['financial'])
    monkeypatch.setattr(SnapshotBuilder, '_inventory', original)

    entry = result.manifest['datasets']['financial']
    assert entry['member_count'] == 1
    assert rows(artifact_file(result, 'financial'), 'fact_versions')[0][0] == first
    assert [row[0] for row in rows(artifact_file(result, 'financial'), 'acceptance_history')] == [1]
    cutoff = result.manifest['created_at'].replace('Z', '+00:00')
    expected = acceptance_hash('financial', frozen_acceptance(captured['rows'], cutoff))
    assert entry['acceptance_history_hash'] == expected
    assert verify_snapshot(tmp_path / 'root', result.snapshot_id)['status'] == 'pass'

    # A later snapshot sees both versions; the earlier artifact remains exactly as built.
    artifacts_before = {path.name: path.read_bytes() for path in (result.directory / 'artifacts').iterdir()}
    later = build(lab, tmp_path / 'root', datasets=['financial'])
    assert later.snapshot_id != result.snapshot_id
    assert later.manifest['datasets']['financial']['member_count'] == 2
    assert len(rows(artifact_file(later, 'financial'), 'fact_versions')) == 2
    assert {path.name: path.read_bytes() for path in (result.directory / 'artifacts').iterdir()} == artifacts_before
    assert verify_snapshot(tmp_path / 'root', result.snapshot_id)['status'] == 'pass'


def test_build_refuses_a_missing_artifact_and_a_corrupted_artifact(lab, tmp_path):
    """ST-M08.2: lost/corrupt files stop verification; no other file is substituted."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    lab.at(datetime(2026, 5, 2, 1, tzinfo=timezone.utc))
    result = build(lab, tmp_path / 'root', datasets=['financial'])

    artifact_file(result, 'financial').unlink()
    with pytest.raises(SnapshotBuildError) as missing:
        verify_snapshot(tmp_path / 'root', result.snapshot_id)
    assert missing.value.code == 'ARTIFACT_MISSING'
    assert missing.value.detail.startswith('financial:')

    other = tmp_path / 'other'
    again = build(lab, other, datasets=['financial'])
    with open(artifact_file(again, 'financial'), 'ab') as handle:
        handle.write(b'not a duckdb checkpoint')
    with pytest.raises(SnapshotBuildError) as corrupt:
        verify_snapshot(other, again.snapshot_id)
    assert corrupt.value.code == 'ARTIFACT_HASH_MISMATCH'

    (again.directory / 'manifest.json').write_text('{tampered', encoding='utf-8')
    with pytest.raises(SnapshotBuildError) as manifest_error:
        verify_snapshot(other, again.snapshot_id)
    assert manifest_error.value.code == 'MANIFEST_INVALID'


def test_build_refuses_when_the_calendar_dependency_is_missing(lab, tmp_path):
    """No calendar means no session closes: the build must not invent them."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    with lab.engine.begin() as db:
        from app.db import governance_master_schema as m
        db.execute(delete(m.versions).where(m.versions.c.kind == 'calendar'))
    with pytest.raises(SnapshotBuildError) as error:
        build(lab, tmp_path / 'root', datasets=['financial'])
    assert error.value.code == 'DEPENDENCY_MISSING'
    assert error.value.detail == 'CALENDAR_VERSION_MISSING'


def test_decimal_payloads_and_fact_identity_are_byte_exact(lab, tmp_path):
    """ST-M08.2: Decimal/哈希一致 -- the artifact carries the writer's exact bytes."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    lab.at(datetime(2026, 5, 2, 1, tzinfo=timezone.utc))
    result = build(lab, tmp_path / 'root', datasets=['financial'])
    with lab.engine.connect() as db:
        stored = db.execute(select(s.fact_versions).where(s.fact_versions.c.data_version == version)).mappings().one()
    con_rows = rows(artifact_file(result, 'financial'), 'fact_versions')
    labels = [column for column in s.fact_versions.c.keys()]
    artifact = dict(zip(labels, con_rows[0]))
    assert artifact['data_version'] == version
    assert artifact['key_json'] == stored['key_json']
    assert artifact['payload_json'] == stored['payload_json']
    payload = json.loads(artifact['payload_json'])
    assert payload['net_profit_parent'] == '100.00000000'
    assert isinstance(payload['net_profit_parent'], str)
    from app.services.governance_fact_contracts import digest, hash_object
    assert digest(artifact['payload_json'].encode('utf-8')) == artifact['normalized_payload_hash']
    identity = {name: artifact[name] for name in ('dataset', 'logical_key', 'source_tag', 'contract_version',
                                                  'definition_id', 'source_revision_token',
                                                  'normalized_payload_hash', 'publication_evidence_hash',
                                                  'supersedes_version')}
    assert hash_object('fact-v1', identity) == version


def test_manifest_pins_master_calendar_and_closes(lab, tmp_path):
    """ST-M08.2: complete dependencies are pinned, not read live at consume time."""
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    lab.daily('2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    lab.at(datetime(2026, 6, 2, 1, tzinfo=timezone.utc))
    result = build(lab, tmp_path / 'root', datasets=['daily_bars'])
    dependencies = result.manifest['dependencies']
    assert dependencies['instrument_version'] == lab.instrument_version
    assert dependencies['calendar_versions'] == [lab.calendar_version]
    closes = dependencies['session_closes']
    assert closes['calendar_version'] == lab.calendar_version
    assert set(closes['closes']) == {'SSE', 'INDEX'}
    document = lab.master.calendar_snapshot(lab.calendar_version)
    expected = {value.isoformat() for value in lab.master.trading_days(
        lab.calendar_version, 'SSE', date.fromisoformat(document['start']), date.fromisoformat(document['end_exclusive']))}
    assert set(closes['closes']['SSE']) == expected
    assert set(closes['closes']['INDEX']) == expected
    masters = rows(artifact_file(result, 'dependencies'), 'master_versions')
    assert {row[0] for row in masters} == {lab.instrument_version, lab.calendar_version}
    assert verify_snapshot(tmp_path / 'root', result.snapshot_id)['status'] == 'pass'


def test_build_carries_a_pinned_adjustment_set(lab, tmp_path):
    """ST-M08.2: an adj factor set is a dependency artifact, not a live lookup."""
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
    factor = prices.freeze_factors(spec, evidence=b'SYNTHETIC M08.2 pinned factor set')
    prices.derive(version, factor)
    lab.at(datetime(2026, 6, 3, 1, tzinfo=timezone.utc))
    result = build(lab, tmp_path / 'root', datasets=['daily_bars'])
    assert result.manifest['dependencies']['adjustment_sets'] == [factor]
    dependency = artifact_file(result, 'dependencies')
    assert [row[0] for row in rows(dependency, 'adjustment_sets')] == [factor]
    assert rows(dependency, 'adjustment_factor_rows')
    assert [row[0] for row in rows(dependency, 'adjustment_set_inputs')] == [factor]
    assert verify_snapshot(tmp_path / 'root', result.snapshot_id)['status'] == 'pass'


def test_build_is_idempotent_and_never_rewrites_an_existing_snapshot(lab, tmp_path):
    """The same frozen inventory and build instant produce the same snapshot id."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    lab.at(datetime(2026, 5, 2, 1, tzinfo=timezone.utc))
    first = build(lab, tmp_path / 'root', datasets=['financial'])
    before = artifact_file(first, 'financial').read_bytes()
    again = build(lab, tmp_path / 'root', datasets=['financial'])
    assert again.snapshot_id == first.snapshot_id and again.reused is True
    assert artifact_file(again, 'financial').read_bytes() == before


def test_manifest_artifact_extension_is_strict():
    """A path index is format-checked: traversal, unknown keys and bad hashes fail."""
    built = build_manifest_fixture()
    for mutation in ('traversal', 'absolute', 'unknown_key', 'bad_hash', 'missing_dependency', 'missing_dataset'):
        with pytest.raises(SnapshotManifestError):
            validate_document(mutate_artifacts(built, mutation))


def build_manifest_fixture():
    from app.services.governance_snapshot_manifest import dataset_entry, manifest_document
    entries = {'financial': dataset_entry(dataset='financial', members=[], acceptance_history_hash='c' * 64,
                                          created_at='2026-05-01T00:00:00.000000Z')}
    dependencies = {'instrument_version': 'd' * 64, 'calendar_versions': ['e' * 64],
                    'session_closes': {'calendar_version': 'e' * 64,
                                       'closes': {'SSE': {'2026-01-01': '2026-01-01T15:00:00+08:00'}},
                                       'evidence_sha256': 'f' * 64},
                    'adjustment_sets': []}
    from app.services.governance_fact_contracts import canonical, digest
    dependencies['session_closes']['evidence_sha256'] = digest(canonical(dependencies['session_closes']['closes']).encode())
    document = manifest_document(created_at='2026-05-01T00:00:00.000000Z', datasets=entries, dependencies=dependencies)
    document['artifacts'] = {'financial': [{'path': 'artifacts/financial.duckdb', 'sha256': '1' * 64}],
                             'dependencies': [{'path': 'artifacts/dependencies.duckdb', 'sha256': '2' * 64}]}
    return validate_document(document)


def mutate_artifacts(document, mutation):
    import copy
    value = copy.deepcopy(document)
    financial = value['artifacts']['financial'][0]
    if mutation == 'traversal':
        financial['path'] = '../outside.duckdb'
    elif mutation == 'absolute':
        financial['path'] = '/tmp/outside.duckdb'
    elif mutation == 'unknown_key':
        financial['rows'] = 1
    elif mutation == 'bad_hash':
        financial['sha256'] = 'not-a-hash'
    elif mutation == 'missing_dependency':
        del value['artifacts']['dependencies']
    elif mutation == 'missing_dataset':
        del value['artifacts']['financial']
    return value
