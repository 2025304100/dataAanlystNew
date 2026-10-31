"""Explicit, additive migration for disposable candidates only; no config lookup.

Normal application startup/Alembic never invokes this module. Production/remote
targets are rejected before opening a connection. No legacy row is rewritten.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re

from sqlalchemy import event, inspect, insert, select
from sqlalchemy import UniqueConstraint

from app.db.governance_schema import metadata

REVISION = 'DSG-R2R3-001'


class MigrationError(ValueError):
    pass


def _candidate(engine):
    url = engine.url
    if url.get_backend_name() == 'mysql':
        if url.host not in {'127.0.0.1', 'localhost'} or not re.fullmatch(r'codex_dsg_[a-z0-9_]{1,48}', url.database or ''):
            raise MigrationError('CANDIDATE_SCOPE_REQUIRED')
    elif url.get_backend_name() == 'sqlite':
        path = Path(url.database or '')
        if not path.is_absolute() or not re.fullmatch(r'codex_dsg_[a-zA-Z0-9_]+\.(sqlite|db)', path.name):
            raise MigrationError('CANDIDATE_SCOPE_REQUIRED')
        def foreign_keys(connection, record, proxy):
            connection.execute('PRAGMA foreign_keys=ON')
        if not getattr(engine, '_governance_fk_enabled', False):
            event.listen(engine, 'checkout', foreign_keys)
            engine._governance_fk_enabled = True
    else:
        raise MigrationError('CANDIDATE_DIALECT_UNSUPPORTED')


def migrate_candidate(engine, *, metadata=metadata, revision=REVISION):
    """Return installed revision/created tables, preserving all pre-existing data."""
    _candidate(engine)
    created = []
    journal = metadata.tables['governance_schema_migrations']
    digest = _schema_hash(engine.dialect, metadata)
    lock_name = 'dsg-migrate:' + hashlib.sha256(engine.url.database.encode('utf-8')).hexdigest()[:40]
    with engine.connect() as db:
        locked = False
        try:
            if db.dialect.name == 'mysql':
                locked = db.exec_driver_sql('SELECT GET_LOCK(%s, 15)', (lock_name,)).scalar() == 1
                db.commit()
                if not locked:
                    raise MigrationError('MIGRATION_BUSY')
            else:
                db.exec_driver_sql('PRAGMA foreign_keys=ON')
                db.commit()
                db.exec_driver_sql('BEGIN IMMEDIATE')
            _preflight(db, metadata)
            existing = set(inspect(db).get_table_names())
            if journal.name in existing:
                row = db.execute(select(journal).where(journal.c.revision == revision)).mappings().first()
                if row and row['schema_hash'] != digest:
                    raise MigrationError('MIGRATION_REVISION_DRIFT')
                if row and row['state'] == 'applied':
                    if not set(metadata.tables) <= existing:
                        raise MigrationError('APPLIED_SCHEMA_INCOMPLETE')
                    db.commit()
                    return {'revision': revision, 'created_tables': [], 'schema_hash': digest}
            else:
                journal.create(db)
                created.append(journal.name)
                if db.dialect.name == 'mysql':
                    db.commit()
            row = db.execute(select(journal).where(journal.c.revision == revision)).mappings().first()
            values = dict(schema_hash=digest, state='applying', completed_at=None, error_code=None)
            if row:
                db.execute(journal.update().where(journal.c.revision == revision).values(**values))
            else:
                db.execute(insert(journal).values(revision=revision, started_at=_now(), **values))
            if db.dialect.name == 'mysql':
                db.commit()
            try:
                for target in metadata.sorted_tables:
                    if target.name not in existing and target.name != journal.name:
                        target.create(db)
                        created.append(target.name)
                        if db.dialect.name == 'mysql':
                            db.commit()  # MySQL DDL commits implicitly; never promise atomic DDL rollback.
                _preflight(db, metadata)
                control = metadata.tables['governance_master_control']
                if db.execute(select(control.c.id)).first() is None:
                    db.execute(insert(control).values(id=1, next_instrument_id=1, instrument_head=None))
                db.execute(journal.update().where(journal.c.revision == revision).values(state='applied', completed_at=_now(), error_code=None))
                db.commit()
            except Exception as error:
                # Keep additive partial DDL for preflight/resume; never drop or alter old objects.
                # A lost connection may prevent this status update; then 'applying' means interrupted.
                try:
                    db.execute(journal.update().where(journal.c.revision == revision).values(state='failed', error_code=type(error).__name__))
                    db.commit()
                except Exception:
                    db.rollback()
                raise
        finally:
            if db.in_transaction():
                db.rollback()
            if locked:
                db.exec_driver_sql('SELECT RELEASE_LOCK(%s)', (lock_name,))
                db.commit()
    return {'revision': revision, 'created_tables': created, 'schema_hash': digest}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _schema_hash(dialect, metadata=metadata):
    # SQLAlchemy stores constraints in sets. Hashing rendered CREATE TABLE can
    # change across Python processes even though the schema is identical.
    tables = []
    for target in sorted(metadata.tables.values(), key=lambda t: t.name):
        tables.append({
            'table': target.name,
            'columns': [(c.name, str(c.type.compile(dialect=dialect)), c.nullable) for c in target.c],
            'pk': [c.name for c in target.primary_key],
            'unique': sorted(tuple(c.name for c in u.columns) for u in target.constraints if isinstance(u, UniqueConstraint)),
            'foreign_keys': sorted((tuple(e.parent.name for e in f.elements), f.referred_table.name,
                tuple(e.column.name for e in f.elements), f.ondelete, f.onupdate) for f in target.foreign_key_constraints),
            'indexes': sorted((i.name, tuple(c.name for c in i.columns), i.unique) for i in target.indexes),
            'mysql': dict(target.dialect_kwargs),
        })
    encoded = json.dumps({'format': 'dsg-schema-fingerprint-v1', 'dialect': dialect.name, 'tables': tables}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def migration_status(engine, *, metadata=metadata, revision=REVISION):
    """Read-only candidate inspection; does not bootstrap an absent journal."""
    _candidate(engine)
    with engine.connect() as db:
        _preflight(db, metadata)
        journal = metadata.tables['governance_schema_migrations']
        if journal.name not in inspect(db).get_table_names():
            return {'revision': revision, 'state': 'not_started'}
        row = db.execute(select(journal).where(journal.c.revision == revision)).mappings().first()
        if not row:
            return {'revision': revision, 'state': 'not_started'}
        if row['schema_hash'] != _schema_hash(db.dialect, metadata):
            raise MigrationError('MIGRATION_REVISION_DRIFT')
        if row['state'] == 'applied' and not set(metadata.tables) <= set(inspect(db).get_table_names()):
            raise MigrationError('APPLIED_SCHEMA_INCOMPLETE')
        return dict(row)


def _type(kind, dialect):
    value = str(kind.compile(dialect=dialect)).upper()
    value = re.split(r'\s+(?:CHARACTER SET|COLLATE)\s+', value)[0]
    value = re.sub(r'^NUMERIC\b', 'DECIMAL', value)
    return re.sub(r'\b(BIGINT|INTEGER|SMALLINT|TINYINT)\(\d+\)', r'\1', value)


def _preflight(db, metadata=metadata):
    """Validate every overlapping object before any DDL or seed mutation."""
    inspector = inspect(db)
    if set(inspector.get_view_names()) & set(metadata.tables):
        raise MigrationError('SCHEMA_DRIFT:view_collision')
    existing = set(inspector.get_table_names())
    for name in sorted(existing & set(metadata.tables)):
        expected = metadata.tables[name]
        actual = {c['name']: c for c in inspector.get_columns(name)}
        drift = set(actual) != set(expected.c.keys())
        options = inspector.get_table_options(name) if db.dialect.name == 'mysql' else {}
        for column in expected.c:
            found = actual.get(column.name)
            if found is None:
                continue
            drift |= found['nullable'] != column.nullable or _type(found['type'], db.dialect) != _type(column.type, db.dialect)
            if db.dialect.name == 'mysql':
                wanted = column.type.dialect_impl(db.dialect)
                collation = getattr(wanted, 'collation', None)
                if collation:
                    drift |= (getattr(found['type'], 'collation', None) or options.get('mysql_collate')) != collation
        drift |= inspector.get_pk_constraint(name)['constrained_columns'] != [c.name for c in expected.primary_key]
        unique = {tuple(c['column_names']) for c in inspector.get_unique_constraints(name)}
        wanted_unique = {tuple(c.name for c in constraint.columns) for constraint in expected.constraints if isinstance(constraint, UniqueConstraint)}
        drift |= unique != wanted_unique
        foreign = inspector.get_foreign_keys(name)
        actual_fk = {(tuple(c['constrained_columns']), c['referred_table'], tuple(c['referred_columns'])) for c in foreign}
        expected_fk = {(tuple(e.parent.name for e in c.elements), c.referred_table.name, tuple(e.column.name for e in c.elements)) for c in expected.foreign_key_constraints}
        drift |= actual_fk != expected_fk
        drift |= any(c['options'].get(rule) not in {None, 'RESTRICT', 'NO ACTION'} for c in foreign for rule in ('ondelete', 'onupdate'))
        if db.dialect.name == 'mysql':
            drift |= options.get('mysql_engine', '').lower() != 'innodb'
            wanted_collation = expected.dialect_options['mysql'].get('collate')
            if wanted_collation:
                drift |= options.get('mysql_collate') != wanted_collation
        if drift:
            raise MigrationError('SCHEMA_DRIFT:' + name)
