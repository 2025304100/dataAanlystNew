"""Replay the frozen 0080 schema inventory without importing current models.

The adjacent JSON is an immutable revision artifact. Future schema changes must
be new revisions, not regenerated snapshots. All unsafe missing-column
backfills are rejected before any schema mutation.
"""
from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa


SNAPSHOT = Path(__file__).with_name("schema_snapshot_0080.json")


def create_frozen_table(conn, name):
    """Create a corrected fresh-install table from immutable revision data."""
    inventory = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["dialects"][conn.dialect.name]
    table = next(t for t in inventory if t["name"] == name)
    ddl = table["create"].rstrip()
    if conn.dialect.name == "mysql":
        ddl += " ENGINE=InnoDB"
    conn.exec_driver_sql(ddl)
    _ensure_foreign_keys(conn, [table])


def apply_frozen_schema(conn):
    dialect = conn.dialect.name
    if dialect not in {"mysql", "sqlite"}:
        raise RuntimeError("Frozen 0080 supports MySQL/SQLite only")
    inventory = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["dialects"][dialect]
    # Its mixed old/new contract is migrated separately with explicit mappings.
    inventory = [t for t in inventory if t["name"] != "idempotency_records"]
    q = conn.dialect.identifier_preparer.quote_identifier
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())
    missing_columns = []
    for table in inventory:
        name = table["name"]
        if name not in existing_tables:
            continue
        current = {c["name"] for c in inspector.get_columns(name)}
        absent = [c for c in table["columns"] if c["name"] not in current]
        populated = bool(absent and conn.exec_driver_sql(f"SELECT 1 FROM {q(name)} LIMIT 1").first())
        for column in absent:
            if column["primary"]:
                raise RuntimeError(f"{name}.{column['name']}: missing primary key needs a dedicated migration")
            if populated and column["required"] and not column["computed"] and column["backfill"] is None:
                raise RuntimeError(f"{name}.{column['name']}: required historical values have no explicit backfill; no DDL applied")
            missing_columns.append((name, column, populated))
    # Validate engines before CREATE/ALTER so an apparent rollback cannot leave
    # partially modified MyISAM trading facts.
    if dialect == "mysql":
        bad = conn.execute(sa.text("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE' "
            "AND ENGINE <> 'InnoDB' AND TABLE_NAME <> 'alembic_version'")).all()
        if bad:
            raise RuntimeError(f"Nontransactional tables need a separate verified engine migration: {bad!r}")
    _ensure_legacy_empty_sync_plan_contract(conn)
    for table in inventory:
        if table["name"] not in existing_tables:
            ddl = table["create"].rstrip()
            if dialect == "mysql":
                ddl += " ENGINE=InnoDB"
            conn.exec_driver_sql(ddl)
    for table, column, populated in missing_columns:
        definition = column["ddl"]
        if populated and column["required"] and not column["computed"] and " DEFAULT " not in f" {definition} ":
            # MySQL 5.7 TEXT/BLOB cannot carry a default; copy the explicit
            # constant, then enforce the target NOT NULL constraint.
            conn.exec_driver_sql(f"ALTER TABLE {q(table)} ADD COLUMN {column['nullable_ddl']}")
            conn.execute(sa.text(f"UPDATE {q(table)} SET {q(column['name'])}=:value "
                                 f"WHERE {q(column['name'])} IS NULL"), {"value": column["backfill"]})
            conn.exec_driver_sql(f"ALTER TABLE {q(table)} MODIFY COLUMN {definition}")
        else:
            conn.exec_driver_sql(f"ALTER TABLE {q(table)} ADD COLUMN {definition}")
    for table in inventory:
        existing = {i["name"]: i for i in sa.inspect(conn).get_indexes(table["name"])}
        for index in table["indexes"]:
            found = existing.get(index["name"])
            if found:
                if found.get("column_names") != index["columns"]:
                    # The member index has a dedicated data-aware replacement.
                    if index["name"] == "idx_portfolio_members_active":
                        from app.db.migration_contracts_v83 import ensure_member_contract
                        ensure_member_contract(conn)
                    else:
                        raise RuntimeError(f"{table['name']}.{index['name']}: existing index differs from frozen contract")
                elif index["unique"] and not found.get("unique"):
                    _strengthen_unique_index(conn, table["name"], index)
                # Keep an already-stronger historical unique index. The ORM
                # may also describe the same invariant as a UniqueConstraint.
                continue
            conn.exec_driver_sql(index["sql"])
    _ensure_foreign_keys(conn, inventory)


def _strengthen_unique_index(conn, table, index):
    q = conn.dialect.identifier_preparer.quote_identifier
    cols = ",".join(q(c) for c in index["columns"])
    present = " AND ".join(f"{q(c)} IS NOT NULL" for c in index["columns"])
    duplicate = conn.exec_driver_sql(f"SELECT 1 FROM {q(table)} WHERE {present} GROUP BY {cols} HAVING COUNT(*)>1 LIMIT 1").first()
    if duplicate:
        raise RuntimeError(f"{table}.{index['name']}: duplicate business keys retained; no automatic deletion")
    bridge = ("v80_" + index["name"])[:64]
    existing = {i["name"] for i in sa.inspect(conn).get_indexes(table)}
    if bridge not in existing:
        conn.exec_driver_sql(f"CREATE UNIQUE INDEX {q(bridge)} ON {q(table)} ({cols})")
    suffix = f" ON {q(table)}" if conn.dialect.name == "mysql" else ""
    conn.exec_driver_sql(f"DROP INDEX {q(index['name'])}{suffix}")
    conn.exec_driver_sql(index["sql"])
    conn.exec_driver_sql(f"DROP INDEX {q(bridge)}{suffix}")


def _ensure_legacy_empty_sync_plan_contract(conn):
    """The retired scheduler config is not equivalent to a request plan.

    Empty legacy tables can be made write-compatible without losing columns.
    Populated configurations require an explicit semantic migration; refuse to
    fabricate request dates/tasks or silently remove those historical records.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    inspector = sa.inspect(conn)
    table = "data_sync_plans"
    if not inspector.has_table(table):
        return
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    if isinstance(columns["id"]["type"], sa.String):
        return
    if conn.exec_driver_sql("SELECT 1 FROM data_sync_plans LIMIT 1").first():
        raise RuntimeError("Legacy data_sync_plans configurations are retained; an explicit request-plan mapping is required")
    operations = Operations(MigrationContext.configure(conn))
    with operations.batch_alter_table(table, recreate="always" if conn.dialect.name == "sqlite" else "auto") as batch:
        batch.alter_column("id", existing_type=columns["id"]["type"], type_=sa.String(64),
                           existing_nullable=False, nullable=False, autoincrement=False)
        for name in ("plan_name", "data_type", "source_type", "cron_expression"):
            if name in columns and not columns[name]["nullable"]:
                batch.alter_column(name, existing_type=columns[name]["type"], nullable=True,
                                   existing_server_default=sa.text(columns[name]["default"]) if columns[name].get("default") is not None else None)


def _ensure_foreign_keys(conn, inventory):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    operations = Operations(MigrationContext.configure(conn))
    q = conn.dialect.identifier_preparer.quote_identifier
    for table in inventory:
        name = table["name"]
        existing = sa.inspect(conn).get_foreign_keys(name)
        missing = []
        for fk in table["foreign_keys"]:
            same = next((old for old in existing if old["constrained_columns"] == fk["columns"]
                         and old["referred_table"] == fk["target"]
                         and old["referred_columns"] == fk["target_columns"]), None)
            if same:
                continue  # Existing delete policies belong to their explicit revisions.
            joins = " AND ".join(f"p.{q(dst)}=c.{q(src)}" for src, dst in zip(fk["columns"], fk["target_columns"]))
            non_null = " AND ".join(f"c.{q(col)} IS NOT NULL" for col in fk["columns"])
            orphan = conn.exec_driver_sql(f"SELECT 1 FROM {q(name)} c LEFT JOIN {q(fk['target'])} p ON {joins} "
                                           f"WHERE {non_null} AND p.{q(fk['target_columns'][0])} IS NULL LIMIT 1").first()
            if orphan:
                raise RuntimeError(f"{name}.{fk['name']}: orphan rows retained; explicit evidence repair required")
            missing.append(fk)
        if not missing:
            continue
        if conn.dialect.name == "sqlite":
            # Batch reflection preserves legacy columns, rows, indexes and constraints.
            with operations.batch_alter_table(name, recreate="always") as batch:
                for fk in missing:
                    batch.create_foreign_key(fk["name"], fk["target"], fk["columns"], fk["target_columns"],
                                             ondelete=fk["ondelete"], onupdate=fk["onupdate"])
        else:
            for fk in missing:
                operations.create_foreign_key(fk["name"], name, fk["target"], fk["columns"], fk["target_columns"],
                                              ondelete=fk["ondelete"], onupdate=fk["onupdate"])
