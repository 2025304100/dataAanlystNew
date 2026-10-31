"""Public migration boundary against disposable databases, never app startup."""
from datetime import date, datetime
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.whitebox


@pytest.fixture(params=['sqlite', 'mysql'] if os.environ.get('GOVERNANCE_SCHEMA_MYSQL_URL') else ['sqlite'])
def candidate(tmp_path, request, record_property):
    dialect = request.param
    if dialect == 'mysql':
        from sqlalchemy.engine import make_url
        configured = os.environ.get('GOVERNANCE_SCHEMA_MYSQL_URL')
        if not configured:
            pytest.fail('MySQL lane requires the isolated M05.2 verification driver')
        url = make_url(configured)
        assert url.host in {'localhost', '127.0.0.1'} and not url.database
        name = 'codex_dsg_' + uuid4().hex[:16]
        admin = create_engine(url, hide_parameters=True)
        with admin.begin() as db:
            version = db.scalar(text('SELECT VERSION()'))
            assert version.startswith('5.7.')
            db.exec_driver_sql(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        engine = create_engine(url.set(database=name), hide_parameters=True)
        record_property('mysql_version', version)
        record_property('isolated_database', name)
    else:
        engine = create_engine('sqlite:///' + (tmp_path / 'codex_dsg_candidate.sqlite').as_posix())
        admin = None
    yield engine
    engine.dispose()
    if admin:
        admin.dispose()  # Retain unique candidate schemas as test evidence.


def migrate(engine):
    from app.db.governance_migration import migrate_candidate
    return migrate_candidate(engine)


def test_empty_candidate_installs_version_identity_and_utc_microseconds(candidate):
    result = migrate(candidate)
    schema = inspect(candidate)
    assert result['revision'] == 'DSG-R2R3-001'
    assert schema.get_pk_constraint('fact_versions')['constrained_columns'] == ['data_version']
    assert schema.get_pk_constraint('fact_keys')['constrained_columns'] == ['dataset', 'logical_key']
    columns = {c['name']: c for c in schema.get_columns('fact_versions')}
    assert set(columns) == {
        'data_version', 'dataset', 'logical_key', 'key_json', 'payload_json', 'raw_hash',
        'contract_version', 'definition_id', 'source_tag', 'field_sources', 'event_date',
        'published_at', 'publication_date', 'publication_precision', 'public_available_at',
        'observed_at', 'valid_from', 'valid_to', 'revision_kind', 'supersedes_version',
        'publication_evidence_hash', 'source_revision_token', 'normalized_payload_hash',
        'source_revision_order', 'source_revision_evidence_hash'}
    assert not columns['observed_at']['nullable'] and columns['published_at']['nullable']
    # available_at and system intervals belong to separate mutable/append-only projections.
    assert 'available_at' not in columns and 'system_from' not in columns
    assert migrate(candidate)['created_tables'] == []


def seed_fact(engine, version='1', logical='a', parent=None):
    """SQL is the public seam here: M05.2 tests constraints, not writer policy."""
    from sqlalchemy import insert
    from app.db.governance_schema import metadata as schema
    with engine.begin() as db:
        if not db.scalar(text('SELECT COUNT(*) FROM raw_evidence')):
            db.execute(insert(schema.tables['raw_evidence']).values(raw_hash='e'*64, raw_bytes=b'wire', source_uri='fixture://raw', observed_at=datetime(2026, 1, 2)))
        if not db.scalar(text('SELECT COUNT(*) FROM fact_keys WHERE logical_key=:k'), {'k': logical*64}):
            db.execute(insert(schema.tables['fact_keys']).values(dataset='daily_bars', logical_key=logical*64, key_json='{}'))
        db.execute(insert(schema.tables['fact_versions']).values(
            data_version=version*64, dataset='daily_bars', logical_key=logical*64, key_json='{}', payload_json='{"raw_close":"1.00000000"}',
            raw_hash='e'*64, contract_version='c'*64, definition_id='d'*64, source_tag='fixture:only', field_sources={},
            event_date=date(2026, 1, 1), published_at=None, publication_date=None, publication_precision='unknown',
            public_available_at=None, observed_at=datetime(2026, 1, 2, 3, 4, 5, 123456), valid_from=date(2026, 1, 1), valid_to=date(2026, 1, 2),
            revision_kind='original' if parent is None else 'correction', supersedes_version=parent,
            publication_evidence_hash=None, source_revision_token='content:'+'f'*64, normalized_payload_hash='f'*64,
            source_revision_order=None, source_revision_evidence_hash=None))
    return version*64


def decision(engine, version, identity='2', logical='a'):
    from sqlalchemy import insert
    from app.db.governance_schema import metadata as schema
    with engine.begin() as db:
        db.execute(insert(schema.tables['source_decisions']).values(
            decision_id=identity*64, dataset='daily_bars', logical_key=logical*64, candidate_version=version, previous_version=None,
            event='T03', from_state='claimed', to_state='canonical', actor='test:review', evidence_hash='e'*64,
            decided_at=datetime(2026, 1, 3), expected_pointer_revision=0, result='accepted'))
    return identity*64


def test_current_and_history_bind_to_same_logical_fact_and_first_availability(candidate):
    from sqlalchemy import insert, select
    from app.db.governance_schema import metadata as schema
    migrate(candidate)
    version = seed_fact(candidate)
    other = seed_fact(candidate, '3', 'b')
    audit = decision(candidate, version)
    availability = schema.tables['version_availability']
    current = schema.tables['fact_current']
    history = schema.tables['acceptance_history']
    with candidate.begin() as db:
        db.execute(insert(availability).values(data_version=version, available_at=datetime(2026, 1, 3), decision_id=audit))
        db.execute(insert(current).values(dataset='daily_bars', logical_key='a'*64, data_version=version, pointer_revision=1,
            source_state='canonical', quality_profile='daily_bars/core', quality_status='complete', provenance_mode='single', updated_at=datetime(2026, 1, 3)))
        db.execute(insert(history).values(dataset='daily_bars', logical_key='a'*64, pointer_revision=1, data_version=version,
            source_state='canonical', quality_status='complete', system_from=datetime(2026, 1, 3), system_to=None, decision_id=audit))
    assert inspect(candidate).get_pk_constraint('acceptance_history')['constrained_columns'] == ['dataset', 'logical_key', 'pointer_revision']
    with candidate.connect() as db:
        assert db.execute(select(history.c.system_to)).scalar_one() is None
        assert db.execute(select(schema.tables['fact_versions'].c.observed_at).where(schema.tables['fact_versions'].c.data_version==version)).scalar_one().microsecond == 123456
    with pytest.raises(IntegrityError), candidate.begin() as db:
        db.execute(current.update().values(data_version=other))
    with pytest.raises(IntegrityError), candidate.begin() as db:
        db.execute(insert(availability).values(data_version=version, available_at=datetime(2026, 1, 4), decision_id=audit))
    with pytest.raises(IntegrityError), candidate.begin() as db:
        db.execute(insert(availability).values(data_version=other, available_at=datetime(2026, 1, 3), decision_id=audit))


def test_snapshot_adjustment_and_outbox_keep_restricting_version_dependencies(candidate):
    migrate(candidate)
    schema = inspect(candidate)
    expected_keys = {
        'write_receipts': ['idempotency_key'], 'fact_observations': ['observation_id'],
        'governance_outbox': ['outbox_id'], 'outbox_members': ['outbox_id', 'data_version'],
        'adjustment_sets': ['adj_factor_version'], 'adjustment_factor_rows': ['adj_factor_version', 'trade_date'],
        'adjusted_bars': ['raw_data_version', 'adj_factor_version'],
        'dataset_versions': ['dataset_version'], 'dataset_members': ['dataset_version', 'data_version'],
        'snapshot_manifest': ['snapshot_id'], 'snapshot_pointer': ['channel'],
        'source_policies': ['policy_version']}
    for name, keys in expected_keys.items():
        assert schema.get_pk_constraint(name)['constrained_columns'] == keys
        for constraint in schema.get_foreign_keys(name):
            # MySQL SHOW CREATE omits RESTRICT (its default); deletion is exercised below.
            assert constraint['options'].get('ondelete') in {None, 'RESTRICT', 'NO ACTION'}
    columns = {c['name']: c for c in schema.get_columns('adjusted_bars')}
    assert (columns['adj_close']['type'].precision, columns['adj_close']['type'].scale) == (24, 8)
    factor = {c['name']: c for c in schema.get_columns('adjustment_factor_rows')}
    assert (factor['factor']['type'].precision, factor['factor']['type'].scale) == (28, 16)
    from sqlalchemy import insert, delete
    from app.db.governance_schema import metadata as model
    version = seed_fact(candidate)
    with candidate.begin() as db:
        db.execute(insert(model.tables['dataset_versions']).values(dataset_version='4'*64, dataset='daily_bars', member_hash='5'*64,
            acceptance_history_hash='6'*64, contract_versions=[], source_policy_versions=[], created_at=datetime(2026, 1, 4)))
        db.execute(insert(model.tables['dataset_members']).values(dataset_version='4'*64, data_version=version))
    with pytest.raises(IntegrityError), candidate.begin() as db:
        db.execute(delete(model.tables['fact_versions']).where(model.tables['fact_versions'].c.data_version==version))
    with pytest.raises(IntegrityError), candidate.begin() as db:
        db.execute(insert(model.tables['dataset_members']).values(dataset_version='4'*64, data_version='9'*64))


def test_schema_name_collision_fails_before_any_candidate_ddl(candidate):
    from app.db.governance_migration import MigrationError
    with candidate.begin() as db:
        db.exec_driver_sql('CREATE TABLE fact_versions (legacy_id INTEGER PRIMARY KEY, payload VARCHAR(40) NOT NULL)')
        db.exec_driver_sql("INSERT INTO fact_versions VALUES (77,'old-result-version')")
    with pytest.raises(MigrationError, match='SCHEMA_DRIFT'):
        migrate(candidate)
    assert inspect(candidate).get_table_names() == ['fact_versions']
    with candidate.connect() as db:
        assert db.exec_driver_sql('SELECT * FROM fact_versions').all() == [(77, 'old-result-version')]


def legacy_fixture(engine):
    from sqlalchemy.orm import Session
    from app.models.symbol import Symbol
    from app.models.daily_bar import DailyBar
    from app.models.score import Score
    from app.models.universe import UniverseSymbol
    tables = [Symbol.__table__, DailyBar.__table__, Score.__table__, UniverseSymbol.__table__]
    for target in tables:
        target.create(engine)
    with Session(engine) as db:
        db.add(Symbol(id=77, symbol='600001', name='保留旧结果', asset_type='stock', market='sh'))
        db.flush()
        db.add(DailyBar(symbol_id=77, trade_date=date(2026, 1, 1), open=1, high=2, low=1, close=2, volume=100))
        db.add(Score(symbol_id=77, trade_date=date(2026, 1, 1), quality_score=70, quality_grade='B', timing_score=60,
            stage='old', action='hold', priority_score=66, calc_batch_id='old-batch'))
        db.commit()
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)')
        db.exec_driver_sql("INSERT INTO alembic_version VALUES ('2026_10_25_0083')")
    return [t.name for t in tables] + ['alembic_version']


def legacy_snapshot(engine, names):
    from sqlalchemy import MetaData, Table, select
    from sqlalchemy.schema import CreateTable
    result = {}
    with engine.connect() as db:
        for name in names:
            target = Table(name, MetaData(), autoload_with=db)
            result[name] = (str(CreateTable(target).compile(dialect=db.dialect)),
                inspect(db).get_indexes(name), db.execute(select(target).order_by(*target.primary_key)).all())
    return result


def test_interrupted_additive_upgrade_resumes_without_old_schema_or_results_changes(candidate):
    from sqlalchemy import event
    from app.db.governance_migration import migration_status
    names = legacy_fixture(candidate)
    before = legacy_snapshot(candidate, names)
    assert migration_status(candidate)['state'] == 'not_started'
    def failure(connection, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith('CREATE TABLE FACT_CURRENT'):
            raise RuntimeError('injected candidate DDL interruption')
    event.listen(candidate, 'before_cursor_execute', failure)
    try:
        with pytest.raises(RuntimeError, match='injected candidate DDL'):
            migrate(candidate)
    finally:
        event.remove(candidate, 'before_cursor_execute', failure)
    assert migration_status(candidate)['state'] == 'failed'
    assert legacy_snapshot(candidate, names) == before
    result = migrate(candidate)
    assert 'fact_current' in result['created_tables']
    assert migration_status(candidate)['state'] == 'applied'
    assert migrate(candidate)['created_tables'] == []
    assert legacy_snapshot(candidate, names) == before


@pytest.mark.parametrize('drift', ['key', 'nullability', 'foreign_key', 'type', 'view'])
def test_existing_governance_object_drift_is_never_silently_skipped(candidate, drift):
    from app.db.governance_migration import MigrationError
    if drift == 'view':
        with candidate.begin() as db:
            db.exec_driver_sql('CREATE VIEW fact_keys AS SELECT 1 AS legacy_id')
    else:
        sql = {
            'key': 'CREATE TABLE fact_keys (dataset VARCHAR(32) NOT NULL, logical_key CHAR(64) NOT NULL, key_json TEXT NOT NULL)',
            'nullability': 'CREATE TABLE fact_keys (dataset VARCHAR(32) NOT NULL, logical_key CHAR(64) NOT NULL, key_json TEXT, PRIMARY KEY(dataset,logical_key))',
            'foreign_key': 'CREATE TABLE outbox_members (outbox_id CHAR(64) NOT NULL, data_version CHAR(64) NOT NULL, PRIMARY KEY(outbox_id,data_version))',
            'type': 'CREATE TABLE governance_master_control (id INTEGER PRIMARY KEY, next_instrument_id VARCHAR(20) NOT NULL, instrument_head VARCHAR(64))',
        }[drift]
        with candidate.begin() as db:
            db.exec_driver_sql(sql)
    before = set(inspect(candidate).get_table_names())
    with pytest.raises(MigrationError, match='SCHEMA_DRIFT'):
        migrate(candidate)
    assert set(inspect(candidate).get_table_names()) == before


@pytest.mark.parametrize('url', [
    'sqlite:///business.db', 'sqlite:///:memory:', 'mysql+pymysql://localhost/gpfx',
    'mysql+pymysql://example.com/codex_dsg_x', 'mysql+pymysql://localhost/codex_dsg_'])
def test_non_candidate_target_is_rejected_before_connecting(url):
    from sqlalchemy import event
    from app.db.governance_migration import MigrationError
    engine = create_engine(url)
    def connected(*args):
        pytest.fail('Scope must be checked before connecting')
    event.listen(engine, 'do_connect', connected)
    try:
        with pytest.raises(MigrationError, match='CANDIDATE_SCOPE_REQUIRED'):
            migrate(engine)
    finally:
        engine.dispose()


def test_existing_master_version_and_permanent_counter_survive_formal_migration(candidate):
    from app.db.governance_master_schema import initialize_master_schema
    from app.services.governance_master_data import MasterDataService
    from tests.test_governance_master_data import publish, member, code, alias
    initialize_master_schema(candidate)
    master = MasterDataService(candidate)
    identity = master.register_identity('authority:security-A', asset_type='stock', exchange='SSE')
    version = publish(master, [member(identity)], [code(identity, '600001')], [alias(identity, 'symbols', 77, '600001')])
    before = master.instrument_snapshot(version)
    migrate(candidate)
    assert master.instrument_snapshot(version) == before
    assert master.resolve_legacy(version, 'symbols', 77, date(2026, 1, 1))['instrument_id'] == identity
    assert master.register_identity('authority:security-B', asset_type='stock', exchange='SSE') > identity


def test_revision_fingerprint_and_completed_schema_cannot_be_silently_repaired(candidate):
    from app.db.governance_migration import MigrationError, migration_status
    migrate(candidate)
    with candidate.begin() as db:
        db.exec_driver_sql("UPDATE governance_schema_migrations SET schema_hash='" + '0'*64 + "'")
    with pytest.raises(MigrationError, match='MIGRATION_REVISION_DRIFT'):
        migrate(candidate)
    with pytest.raises(MigrationError, match='MIGRATION_REVISION_DRIFT'):
        migration_status(candidate)


def test_completed_migration_with_missing_table_is_rejected(candidate):
    from app.db.governance_migration import MigrationError
    migrate(candidate)
    with candidate.begin() as db:
        db.exec_driver_sql('DROP TABLE snapshot_pointer')
    with pytest.raises(MigrationError, match='APPLIED_SCHEMA_INCOMPLETE'):
        migrate(candidate)


def test_parent_fk_refuses_other_business_key_and_missing_parent(candidate):
    migrate(candidate)
    seed_fact(candidate)
    with pytest.raises(IntegrityError):
        seed_fact(candidate, '2', 'b', '1'*64)
    with pytest.raises(IntegrityError):
        seed_fact(candidate, '3', 'a', '9'*64)
    assert seed_fact(candidate, '4', 'a', '1'*64) == '4'*64
    with pytest.raises(IntegrityError):
        seed_fact(candidate, '4', 'a', '1'*64)


def test_derived_price_refuses_factor_set_base_date_mismatch(candidate):
    from sqlalchemy import insert
    from app.db.governance_schema import metadata as schema
    from app.services.governance_master_data import MasterDataService
    migrate(candidate)
    identity = MasterDataService(candidate).register_identity('authority:price-security', asset_type='stock', exchange='SSE')
    raw = seed_fact(candidate)
    with candidate.begin() as db:
        db.execute(insert(schema.tables['adjustment_sets']).values(adj_factor_version='2'*64, instrument_id=identity,
            base_date=date(2026, 1, 3), as_of=datetime(2026, 1, 4), knowledge_mode='system_known',
            algorithm_version='event-chain-v1', event_version_ids=[], factor_rows_hash='3'*64))
    with pytest.raises(IntegrityError), candidate.begin() as db:
        db.execute(insert(schema.tables['adjusted_bars']).values(raw_data_version=raw, adj_factor_version='2'*64,
            instrument_id=identity, trade_date=date(2026, 1, 1), base_date=date(2026, 1, 2), adj_open='1.00000000',
            adj_high='1.00000000', adj_low='1.00000000', adj_close='1.00000000'))


def test_migration_is_not_registered_with_live_base_or_default_alembic(candidate):
    from app.db.base import Base
    from app.db.governance_schema import metadata
    import app.models  # existing model auto-discovery
    assert not set(metadata.tables) & set(Base.metadata.tables)
    assert not any('governance_migration' in p.read_text(encoding='utf-8') or 'governance_schema' in p.read_text(encoding='utf-8')
        for p in (__import__('pathlib').Path(__file__).resolve().parents[1] / 'alembic').rglob('*.py'))
    assert inspect(candidate).get_table_names() == []


def test_concurrent_candidate_migrations_serialize_and_do_not_reset_identity_counter(candidate):
    from concurrent.futures import ThreadPoolExecutor
    from app.services.governance_master_data import MasterDataService
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: migrate(candidate), range(2)))
    assert sorted(bool(r['created_tables']) for r in results) == [False, True]
    master = MasterDataService(candidate)
    first = master.register_identity('authority:one', asset_type='stock', exchange='SSE')
    migrate(candidate)
    assert master.register_identity('authority:two', asset_type='stock', exchange='SSE') > first


@pytest.mark.parametrize('candidate', ['mysql'], indirect=True)
def test_mysql_connection_loss_leaves_old_results_and_resumable_candidate(candidate):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from app.db.governance_migration import migration_status
    names = legacy_fixture(candidate)
    before = legacy_snapshot(candidate, names)
    admin = create_engine(candidate.url.set(database=None), hide_parameters=True)
    killed = []
    def disconnect(connection, cursor, statement, params, context, many):
        if not killed and statement.lstrip().upper().startswith('CREATE TABLE FACT_CURRENT'):
            worker = connection.connection.driver_connection.thread_id()
            with admin.begin() as db:
                actual = db.scalar(text('SELECT DB FROM information_schema.PROCESSLIST WHERE ID=:id'), {'id': worker})
                assert actual == candidate.url.database and actual.startswith('codex_dsg_')
                db.exec_driver_sql(f'KILL CONNECTION {int(worker)}')
            killed.append(worker)
    event.listen(candidate, 'before_cursor_execute', disconnect)
    try:
        with pytest.raises(OperationalError):
            migrate(candidate)
    finally:
        event.remove(candidate, 'before_cursor_execute', disconnect)
        admin.dispose()
    assert len(killed) == 1
    assert migration_status(candidate)['state'] == 'applying'
    assert legacy_snapshot(candidate, names) == before
    migrate(candidate)
    assert migration_status(candidate)['state'] == 'applied'
    assert legacy_snapshot(candidate, names) == before


def test_operator_cli_requires_explicit_candidate_and_can_inspect_without_reinstalling(candidate):
    import json, subprocess, sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'scripts/migrate_governance_candidate.py'
    env = dict(os.environ, M052_CANDIDATE_URL=candidate.url.render_as_string(hide_password=False))
    commands = ([sys.executable, str(script), '--url-env', 'M052_CANDIDATE_URL'],
                [sys.executable, str(script), '--url-env', 'M052_CANDIDATE_URL', '--inspect'])
    for command in commands:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload['revision'] == 'DSG-R2R3-001'
    assert payload['state'] == 'applied'


@pytest.mark.parametrize('candidate', ['sqlite'], indirect=True)
def test_preexisting_pooled_sqlite_connections_cannot_bypass_foreign_keys(candidate):
    first, second = candidate.connect(), candidate.connect()
    first.close()
    try:
        migrate(candidate)
    finally:
        second.close()
    with candidate.connect() as a, candidate.connect() as b:
        assert [db.exec_driver_sql('PRAGMA foreign_keys').scalar() for db in (a, b)] == [1, 1]
