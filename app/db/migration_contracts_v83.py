"""Frozen corrective contracts for revisions 0079/0081/0083.

Do not import runtime ORM metadata here. Later contract changes need a new
versioned helper/revision so replay of historical revisions remains stable.
Every data-dependent precondition is checked before destructive schema work.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlalchemy as sa


def _quote(conn, name):
    return conn.dialect.identifier_preparer.quote_identifier(name)


def require_transactional_table(conn, table):
    if conn.dialect.name == "mysql":
        engine = conn.execute(sa.text("SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table"), {"table": table}).scalar_one_or_none()
        if engine is not None and engine != "InnoDB":
            raise RuntimeError(f"{table} uses {engine}; a separate verified InnoDB conversion is required")


def ensure_member_contract(conn):
    table = "portfolio_members"
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return
    require_transactional_table(conn, table)
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    required = {"portfolio_id", "symbol_id", "effective_to"}
    if not required <= columns.keys():
        raise RuntimeError("portfolio_members lacks SCD2 keys; repair the historical schema first")
    duplicates = conn.exec_driver_sql(
        "SELECT portfolio_id,symbol_id,COUNT(*) FROM portfolio_members "
        "WHERE effective_to IS NULL GROUP BY portfolio_id,symbol_id HAVING COUNT(*)>1 LIMIT 10"
    ).all()
    if duplicates:
        raise RuntimeError(f"Current member duplicates require an evidence-preserving repair: {duplicates!r}")
    slot = columns.get("active_member_slot")
    if slot is not None:
        expression = (slot.get("computed") or {}).get("sqltext", "")
        normal = re.sub(r"[\s`()]+", "", expression).lower()
        if normal not in {"casewheneffective_toisnullthen1elsenullend", "casewhenisnulleffective_tothen1elsenullend"}:
            raise RuntimeError("active_member_slot exists with an unrecognized generated expression")
    else:
        conn.exec_driver_sql(
            "ALTER TABLE portfolio_members ADD COLUMN active_member_slot INTEGER "
            "GENERATED ALWAYS AS (CASE WHEN effective_to IS NULL THEN 1 ELSE NULL END) VIRTUAL"
        )
    target = "idx_portfolio_members_active"
    bridge = "uq_portfolio_members_current_v83"
    wanted = ["portfolio_id", "symbol_id", "active_member_slot"]
    indexes = {i["name"]: i for i in sa.inspect(conn).get_indexes(table)}

    def correct(index):
        return bool(index and index.get("unique") and index.get("column_names") == wanted)

    def create(name):
        conn.exec_driver_sql(f"CREATE UNIQUE INDEX {_quote(conn, name)} ON portfolio_members "
                             "(portfolio_id,symbol_id,active_member_slot)")

    def drop(name):
        suffix = " ON portfolio_members" if conn.dialect.name == "mysql" else ""
        conn.exec_driver_sql(f"DROP INDEX {_quote(conn, name)}{suffix}")

    if not correct(indexes.get(target)):
        if target in indexes:
            # Keep correct uniqueness continuously while replacing the old index.
            if bridge in indexes and not correct(indexes[bridge]):
                raise RuntimeError("Member migration bridge index has an unexpected definition")
            if bridge not in indexes:
                create(bridge)
            drop(target)
        create(target)
    if bridge in {i["name"] for i in sa.inspect(conn).get_indexes(table)}:
        drop(bridge)


def ensure_idempotency_contract(conn):
    """Preserve legacy columns while making old and new writers interoperable.

    Missing response/request evidence fails before DDL. Derived columns copy
    existing evidence; a missing correlation receives an explicitly labelled,
    stable migration identity, never an invented trading/audit event identity.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    table = "idempotency_records"
    inspector = sa.inspect(conn)
    dialect = conn.dialect.name
    if dialect not in {"mysql", "sqlite"}:
        raise RuntimeError("Idempotency corrective migration supports MySQL and SQLite only")
    if not inspector.has_table(table):
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if dialect == "sqlite" else "INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY"
        engine_clause = " ENGINE=InnoDB" if dialect == "mysql" else ""
        conn.exec_driver_sql(f"""CREATE TABLE idempotency_records (
            id {pk}, idempotency_key VARCHAR(128) NOT NULL,
            resource_type VARCHAR(64) NOT NULL DEFAULT 'general', resource_id VARCHAR(128),
            status VARCHAR(32) NOT NULL DEFAULT 'SUCCEEDED', expires_at DATETIME,
            entity_type VARCHAR(32) NOT NULL, entity_id VARCHAR(64),
            request_hash VARCHAR(128) NOT NULL, response_json TEXT NOT NULL,
            correlation_id VARCHAR(64) NOT NULL, portfolio_id INTEGER,
            expire_at DATETIME, executed_at DATETIME NOT NULL, created_at DATETIME NOT NULL
        ){engine_clause}""")
        inspector = sa.inspect(conn)
    require_transactional_table(conn, table)
    cols = {c["name"]: c for c in inspector.get_columns(table)}
    if "idempotency_key" not in cols:
        raise RuntimeError("Cannot reconstruct idempotency business keys")
    duplicates = conn.exec_driver_sql(
        "SELECT idempotency_key,COUNT(*) FROM idempotency_records GROUP BY idempotency_key "
        "HAVING idempotency_key IS NULL OR idempotency_key='' OR COUNT(*)>1 LIMIT 10"
    ).all()
    if duplicates:
        raise RuntimeError(f"Invalid/duplicate idempotency keys require manual evidence review: {duplicates!r}")
    # Complete evidence validation before DDL, retaining at most one response
    # in memory. Writers must remain stopped across both passes: MySQL DDL
    # commits implicitly and cannot provide a transaction-spanning snapshot.
    original_count = 0
    result = conn.execution_options(stream_results=True).execute(sa.text("SELECT * FROM idempotency_records"))
    try:
        for row in result.mappings():
            _idempotency_backfill_values(row, cols)
            original_count += 1
    finally:
        result.close()
        conn.execution_options(stream_results=False)
    if "id" in cols:
        invalid_ids = conn.exec_driver_sql(
            "SELECT id,COUNT(*) FROM idempotency_records WHERE id IS NOT NULL "
            "GROUP BY id HAVING COUNT(*)>1 OR id<=0 LIMIT 10"
        ).all()
        if invalid_ids:
            raise RuntimeError(f"Existing surrogate IDs conflict; they will not be renumbered: {invalid_ids!r}")
    additions = {
        "resource_type": "VARCHAR(64)", "resource_id": "VARCHAR(128)",
        "status": "VARCHAR(32)", "expires_at": "DATETIME",
        "entity_type": "VARCHAR(32)", "entity_id": "VARCHAR(64)",
        "request_hash": "VARCHAR(128)", "response_json": "TEXT",
        "correlation_id": "VARCHAR(64)", "portfolio_id": "INTEGER",
        "expire_at": "DATETIME", "executed_at": "DATETIME", "created_at": "DATETIME",
    }
    for name, typ in additions.items():
        if name not in cols:
            if dialect == "mysql" and typ == "DATETIME":
                typ = "DATETIME(6)"
            conn.exec_driver_sql(f"ALTER TABLE idempotency_records ADD COLUMN {_quote(conn, name)} {typ} NULL")
    if dialect == "mysql":
        # Widen derived timestamp targets before copying source evidence.
        for name in ("executed_at", "expire_at", "expires_at"):
            definition = next(c for c in sa.inspect(conn).get_columns(table) if c["name"] == name)
            if getattr(definition["type"], "fsp", None) != 6:
                nullable = "NULL" if definition["nullable"] else "NOT NULL"
                conn.exec_driver_sql(f"ALTER TABLE idempotency_records MODIFY {_quote(conn, name)} DATETIME(6) {nullable}")
    last_key = None
    while True:
        predicate = "WHERE idempotency_key > :last" if last_key is not None else ""
        keys = conn.execute(sa.text(f"SELECT idempotency_key FROM idempotency_records {predicate} "
                                     "ORDER BY idempotency_key LIMIT 200"), {"last": last_key}).scalars().all()
        if not keys:
            break
        for key in keys:
            # Close the point-read before writing; an unbuffered MySQL result
            # cannot share its connection with an UPDATE in flight.
            row = conn.execute(sa.text("SELECT * FROM idempotency_records WHERE idempotency_key=:key"), {"key": key}).mappings().one()
            values = _idempotency_backfill_values(row, cols)
            if values:
                assignments = ",".join(f"{_quote(conn, k)}=:v{i}" for i, k in enumerate(values))
                conn.execute(sa.text(f"UPDATE idempotency_records SET {assignments} WHERE idempotency_key=:key"),
                             {"key": key, **{f"v{i}": value for i, value in enumerate(values.values())}})
        last_key = keys[-1]
    # Ensure uniqueness before replacing a legacy primary key, including a
    # restarted migration whose PK has already been removed.
    key_index = "ix_idempotency_records_idempotency_key"
    indexes = {i["name"]: i for i in sa.inspect(conn).get_indexes(table)}
    if key_index in indexes:
        idx = indexes[key_index]
        if not idx.get("unique") or idx.get("column_names") != ["idempotency_key"]:
            raise RuntimeError("Existing idempotency key index has a conflicting definition")
    else:
        conn.exec_driver_sql(f"CREATE UNIQUE INDEX {key_index} ON idempotency_records(idempotency_key)")
    pk_cols = sa.inspect(conn).get_pk_constraint(table).get("constrained_columns") or []
    if dialect == "mysql":
        if "id" not in cols:
            clauses = (["DROP PRIMARY KEY"] if pk_cols else []) + ["ADD COLUMN id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY"]
            conn.exec_driver_sql("ALTER TABLE idempotency_records " + ", ".join(clauses))
        else:
            _fill_missing_surrogate_ids(conn)
            if pk_cols != ["id"]:
                clauses = (["DROP PRIMARY KEY"] if pk_cols else []) + ["MODIFY id INTEGER NOT NULL AUTO_INCREMENT", "ADD PRIMARY KEY (id)"]
                conn.exec_driver_sql("ALTER TABLE idempotency_records " + ", ".join(clauses))
            elif not cols["id"].get("autoincrement"):
                conn.exec_driver_sql("ALTER TABLE idempotency_records MODIFY id INTEGER NOT NULL AUTO_INCREMENT")
        # Defaults keep unmapped/direct legacy writers safe; the service also
        # dual-writes the real resource fields, not just generic placeholders.
        current_types = {c["name"]: str(c["type"].compile(dialect=conn.dialect)) for c in sa.inspect(conn).get_columns(table)}
        conn.exec_driver_sql("ALTER TABLE idempotency_records "
            f"MODIFY resource_type {current_types['resource_type']} NOT NULL DEFAULT 'general', "
            f"MODIFY status {current_types['status']} NOT NULL DEFAULT 'SUCCEEDED', "
            f"MODIFY entity_type {current_types['entity_type']} NOT NULL, "
            f"MODIFY request_hash {current_types['request_hash']} NOT NULL, "
            f"MODIFY response_json {current_types['response_json']} NOT NULL, "
            f"MODIFY correlation_id {current_types['correlation_id']} NOT NULL, "
            f"MODIFY executed_at {current_types['executed_at']} NOT NULL, "
            f"MODIFY created_at {current_types['created_at']} NOT NULL")
    else:
        if "id" not in cols:
            conn.exec_driver_sql("ALTER TABLE idempotency_records ADD COLUMN id INTEGER")
        _fill_missing_surrogate_ids(conn)
        current = sa.inspect(conn)
        pk = current.get_pk_constraint(table)
        required = ["id", "idempotency_key", "resource_type", "status", "entity_type", "request_hash", "response_json", "correlation_id", "executed_at", "created_at"]
        definitions = {c["name"]: c for c in current.get_columns(table)}
        needs_rebuild = pk.get("constrained_columns") != ["id"] or any(definitions[n]["nullable"] for n in required)
        if needs_rebuild:
            operations = Operations(MigrationContext.configure(conn))
            with operations.batch_alter_table(table, recreate="always", naming_convention={"pk": "pk_%(table_name)s"}) as batch:
                if pk.get("constrained_columns"):
                    batch.drop_constraint(pk.get("name") or "pk_idempotency_records", type_="primary")
                for name in required:
                    kwargs = {"nullable": False, "existing_type": definitions[name]["type"]}
                    if name == "resource_type":
                        kwargs["server_default"] = "general"
                    elif name == "status":
                        kwargs["server_default"] = "SUCCEEDED"
                    batch.alter_column(name, **kwargs)
                batch.create_primary_key("pk_idempotency_records_id", ["id"])
    if conn.exec_driver_sql("SELECT COUNT(*) FROM idempotency_records").scalar_one() != original_count:
        raise RuntimeError("Idempotency row count changed during corrective migration; writers must be stopped")
    from app.db.migration_schema_v80 import SNAPSHOT, _ensure_foreign_keys
    frozen = next(t for t in json.loads(SNAPSHOT.read_text(encoding="utf-8"))["dialects"][dialect] if t["name"] == table)
    have_indexes = {i["name"] for i in sa.inspect(conn).get_indexes(table)}
    for index in frozen["indexes"]:
        if index["name"] not in have_indexes:
            conn.exec_driver_sql(index["sql"])
    available = set(sa.inspect(conn).get_table_names())
    frozen["foreign_keys"] = [fk for fk in frozen["foreign_keys"] if fk["target"] in available]
    _ensure_foreign_keys(conn, [frozen])


def _idempotency_backfill_values(row, original_columns):
    key = row["idempotency_key"]
    response = row.get("response_json")
    if response is None:
        response = row.get("result_json")
    try:
        json.loads(response)
    except (TypeError, ValueError):
        raise RuntimeError(f"{key}: original response evidence is missing or invalid; no placeholder response is permitted") from None
    if not row.get("request_hash") or not row.get("created_at"):
        raise RuntimeError(f"{key}: request hash/created_at evidence missing")
    for current, legacy in (("entity_type", "resource_type"), ("entity_id", "resource_id"), ("expire_at", "expires_at")):
        left, right = row.get(current), row.get(legacy)
        if left not in (None, "") and right not in (None, "") and left != right:
            raise RuntimeError(f"{key}: conflicting {current}/{legacy}; preserve both and resolve from original evidence")
    entity_type = row.get("entity_type") or row.get("resource_type")
    entity_id = row.get("entity_id") or row.get("resource_id")
    if not entity_type or len(entity_type) > 32 or (entity_id is not None and len(entity_id) > 64):
        raise RuntimeError(f"{key}: entity provenance does not fit the new contract; no truncation allowed")
    values = {
        "entity_type": entity_type, "entity_id": entity_id,
        "expire_at": row.get("expire_at") or row.get("expires_at"),
        "executed_at": row.get("executed_at") or row["created_at"],
        "correlation_id": row.get("correlation_id") or "migration-0081:" + hashlib.sha256(str(key).encode()).hexdigest()[:48],
        "response_json": response,
        "resource_type": row.get("resource_type") or entity_type,
        "resource_id": row.get("resource_id") or entity_id,
        "status": row.get("status") or "SUCCEEDED",
        "expires_at": row.get("expires_at") or row.get("expire_at"),
    }
    return {k: v for k, v in values.items() if k not in original_columns or row.get(k) is None or row.get(k) == ""}


def _fill_missing_surrogate_ids(conn):
    next_id = int(conn.exec_driver_sql("SELECT COALESCE(MAX(id),0) FROM idempotency_records").scalar_one())
    while True:
        missing = conn.exec_driver_sql("SELECT idempotency_key FROM idempotency_records WHERE id IS NULL ORDER BY idempotency_key LIMIT 200").scalars().all()
        if not missing:
            break
        for key in missing:
            next_id += 1
            conn.execute(sa.text("UPDATE idempotency_records SET id=:id WHERE idempotency_key=:key AND id IS NULL"), {"id": next_id, "key": key})


def ensure_backtest_legacy_write_columns(conn):
    """Keep retired fill evidence while allowing current aggregate-lot writes.

    A legacy fill is not enough to infer a complete aggregate trade. Existing
    rows must already have valid lot keys/entry evidence; missing facts are a
    deployment blocker. Only unused NOT NULL constraints are relaxed.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    table = "backtest_trades"
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return
    definitions = {c["name"]: c for c in inspector.get_columns(table)}
    legacy = [name for name in ("backtest_run_id", "trade_date", "decision_at", "side", "price", "updated_at")
              if name in definitions and not definitions[name]["nullable"]]
    if not legacy:
        return
    require_transactional_table(conn, table)
    populated = conn.exec_driver_sql("SELECT 1 FROM backtest_trades LIMIT 1").first()
    if populated:
        required = {"run_id", "symbol_id", "entry_date", "entry_price", "quantity"}
        if not required <= definitions.keys():
            raise RuntimeError("Legacy backtest fill rows need an explicit aggregate-lot evidence mapping; no data is invented")
        invalid = conn.exec_driver_sql("SELECT id FROM backtest_trades WHERE run_id IS NULL OR run_id<=0 "
            "OR symbol_id IS NULL OR symbol_id<=0 OR entry_date IS NULL OR entry_price IS NULL "
            "OR entry_price<=0 OR quantity IS NULL OR quantity<=0 LIMIT 10").all()
        if invalid:
            raise RuntimeError(f"Legacy backtest lot evidence is incomplete; preserve and repair from source: {invalid!r}")
    operations = Operations(MigrationContext.configure(conn))
    with operations.batch_alter_table(table, recreate="always" if conn.dialect.name == "sqlite" else "auto") as batch:
        for name in legacy:
            column = definitions[name]
            batch.alter_column(name, existing_type=column["type"], nullable=True,
                existing_server_default=sa.text(column["default"]) if column.get("default") is not None else None)


def ensure_order_review_contract(conn):
    """Unreviewed orders have NULL status; retain all existing review evidence.

    The retired migration made this optional field NOT NULL and quoted NONE
    twice. Do not convert old states or invent APPROVED reviews. Keep the old
    column width, remove its default, and permit actual current review states.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    table = "sim_orders"
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return
    require_transactional_table(conn, table)
    column = next((c for c in inspector.get_columns(table) if c["name"] == "review_status"), None)
    if column is None:
        raise RuntimeError("sim_orders.review_status is missing; execute its prerequisite schema migration")
    check_name = "ck_sim_orders_review_status_values"
    old_check = next((c for c in inspector.get_check_constraints(table) if c.get("name") == check_name), None)
    needs_check = old_check is not None and ("PENDING_REVIEW" not in old_check["sqltext"] or "TIMED_OUT_CANCELLED" not in old_check["sqltext"])
    if column["nullable"] and column.get("default") is None and not needs_check:
        return
    operations = Operations(MigrationContext.configure(conn))
    with operations.batch_alter_table(table, recreate="always" if conn.dialect.name == "sqlite" else "auto") as batch:
        batch.alter_column("review_status", existing_type=column["type"], nullable=True, server_default=None)
        if needs_check:
            batch.drop_constraint(check_name, type_="check")
            batch.create_check_constraint(check_name,
                "review_status IN ('NONE','PENDING','APPROVED','REJECTED','ESCALATED','PENDING_REVIEW','TIMED_OUT_CANCELLED')")


def ensure_retired_combination_columns(conn):
    """Relax only verified retired write requirements, retaining their evidence.

    Current orders use portfolio_id; Review writes date ranges and optional
    title/update time; tail proxy writers use trade_date and measured proxy
    fields. No old account IDs, review dates or accumulation measures may be
    invented. Legacy columns, types, defaults, indexes and FKs remain intact.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    approved = {
        "sim_orders": ("sim_account_id",),
        "portfolio_reviews": ("review_type", "review_date", "title", "updated_at"),
        "tail_accumulation_snapshots": ("snapshot_date", "accumulation_days", "accumulation_score"),
    }
    operations = Operations(MigrationContext.configure(conn))
    for table, names in approved.items():
        inspector = sa.inspect(conn)
        if not inspector.has_table(table):
            continue
        definitions = {c["name"]: c for c in inspector.get_columns(table)}
        changes = [definitions[name] for name in names if name in definitions and not definitions[name]["nullable"]]
        if not changes:
            continue
        require_transactional_table(conn, table)
        with operations.batch_alter_table(table, recreate="always" if conn.dialect.name == "sqlite" else "auto") as batch:
            for column in changes:
                batch.alter_column(column["name"], existing_type=column["type"], nullable=True,
                    existing_server_default=sa.text(column["default"]) if column.get("default") is not None else None)
