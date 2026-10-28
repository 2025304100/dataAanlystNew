"""Guarded exact-0092 deployment with full-row digests; no app initialization.

check is read-only. upgrade requires passing tests for the exact migration file,
an explicit database, 0091 precondition, cooperative writer locks and a SQL
allowlist. Only six payload columns and the Alembic version may change.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.guarded_database_migration import upgrade_verified
from scripts.review_migration_integrity import digest_table
from app.services.market_event_collection_lock import market_event_write_lock

REV91 = "wps_0024_091_market_compat"
REV92 = "wps_0024_092_market_signals"
REVISION_FILE = ROOT/"alembic/versions/2026_10_27_0092_market_signals.py"
FIELDS = {"market_signal_evidence": ("text",), "market_signal_assessments": ("analysis_json", "input_json"),
          "market_signal_predictions": ("snapshot_json", "outcome_json"), "market_signal_outcomes": ("result_json",)}
TABLES = sorted([*FIELDS, "market_signal_rules", "market_signal_rule_versions", "market_signal_settings"])
NAMESPACES = ("market-signals-monitor", "market-signals", "market-signals-engine", "market-signals-settings", "market-events")


def fingerprint(connection, database):
    row = connection.execute(text("SELECT DATABASE() AS db,VERSION() AS version,@@server_uuid AS uuid")).mappings().one()
    if row["db"] != database or not row["version"].startswith("5.7."):
        raise RuntimeError("Unexpected database identity or MySQL version")
    return {"database":row["db"], "server_version":row["version"],
        "server_uuid_sha256":hashlib.sha256(row["uuid"].encode()).hexdigest(),
        "revisions":list(connection.scalars(text("SELECT version_num FROM alembic_version ORDER BY version_num")))}


def state(connection):
    inspector = inspect(connection)
    types, contracts, rows = {}, {}, {}
    storage = dict(connection.execute(text("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'market_signal_%'")).all())
    for table in TABLES:
        if storage.get(table) != "InnoDB":
            raise RuntimeError(f"Missing or non-InnoDB signal table: {table}")
        columns = inspector.get_columns(table)
        keys = inspector.get_pk_constraint(table).get("constrained_columns")
        if keys != ["id"]:
            raise RuntimeError(f"Unexpected primary key: {table}")
        rows[table] = digest_table(connection, table, [item["name"] for item in columns], keys)
        connection.execution_options(stream_results=False)
        types[table] = {item["name"]:str(item["type"]) for item in columns}
        contracts[table] = {"primary":inspector.get_pk_constraint(table), "indexes":inspector.get_indexes(table),
            "unique":inspector.get_unique_constraints(table), "foreign_keys":inspector.get_foreign_keys(table)}
    column_contract = [dict(row) for row in connection.execute(text(
        "SELECT TABLE_NAME,COLUMN_NAME,IS_NULLABLE,COLUMN_DEFAULT,CHARACTER_SET_NAME,COLLATION_NAME,COLUMN_COMMENT,EXTRA "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'market_signal_%' "
        "ORDER BY TABLE_NAME,ORDINAL_POSITION")).mappings()]
    return {"data":rows, "column_types":types, "column_contract":column_contract, "constraints":contracts, "engines":storage}


def validate_before(connection, database):
    identity = fingerprint(connection, database)
    if identity["revisions"] != [REV91]:
        raise RuntimeError("Expected exactly 0091; refusing other upgrades or a repeated business deployment")
    cfg = Config(str(ROOT/"alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT/"alembic"))
    script = ScriptDirectory.from_config(cfg)
    if script.get_heads() != [REV92] or [item.revision for item in script.iterate_revisions(REV92, REV91)] != [REV92]:
        raise RuntimeError("Expected exactly one pending revision: 0092")
    width = connection.scalar(text("SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='alembic_version' AND COLUMN_NAME='version_num'"))
    if width is None or width < 128:
        raise RuntimeError("Version table needs separate review; refusing an unrelated metadata alteration")
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "upgrade"))
    parser.add_argument("--connection-config", type=Path, required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--validation-result", type=Path)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    folder = args.evidence_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    output = folder/"business-upgrade.json"
    if output.exists():
        parser.error("Use a new evidence directory; previous audit results are never overwritten")
    cfg = json.loads(args.connection_config.read_text(encoding="utf-8-sig"))
    mysql = cfg.get("mysql", {})
    if (not cfg.get("use_mysql") or mysql.get("host") not in ("localhost", "127.0.0.1", "::1")
            or not (args.expected_database == mysql.get("database")
                    or re.fullmatch(r"codex_market_[a-z0-9_]{1,48}", args.expected_database))):
        parser.error("Expected the configured loopback business database or an explicit isolated test schema")
    revision_hash = hashlib.sha256(REVISION_FILE.read_bytes()).hexdigest()
    proof_identity = None
    if args.mode == "upgrade":
        if not args.validation_result:
            parser.error("Passing validation of this exact migration is required")
        proof = json.loads(args.validation_result.read_text(encoding="utf-8"))
        if not proof.get("passed") or proof.get("migration_sha256") != revision_hash:
            parser.error("Validation failed or was run against a different migration file")
        for name in ("signals-existing-text-upgrade.json", "signals-existing-text-roundtrip.json"):
            if not (args.validation_result.parent/name).is_file():
                parser.error("Both existing-data and rollback/large-payload checkpoints are required")
        proof_identity = json.loads((args.validation_result.parent/"identity.json").read_text(encoding="utf-8"))
    url = URL.create("mysql+pymysql", host=mysql["host"], port=int(mysql.get("port",3306)),
        username=mysql["user"], password=mysql.get("password", ""), database=args.expected_database, query={"charset":"utf8mb4"})
    options = {"connect_timeout":10, "read_timeout":45, "write_timeout":30}
    if args.mode == "check":
        options["init_command"] = "SET SESSION TRANSACTION READ ONLY"
    engine = create_engine(url, hide_parameters=True, pool_size=8, max_overflow=2, connect_args=options)
    report = {"started_at":datetime.now(timezone.utc).isoformat(), "passed":False, "mode":args.mode,
        "target_revision":REV92, "database":args.expected_database, "migration_sha256":revision_hash,
        "no_service_restart":True, "executed_mutations":[]}
    try:
        with ExitStack() as stack:
            if args.mode == "upgrade":
                lock_session = stack.enter_context(Session(engine))
                for namespace in NAMESPACES:
                    stack.enter_context(market_event_write_lock(lock_session, namespace=namespace))
                report["writer_locks"] = list(NAMESPACES)
            connection = stack.enter_context(engine.connect())
            report["before_identity"] = validate_before(connection, args.expected_database)
            if proof_identity and report["before_identity"]["server_uuid_sha256"] != proof_identity["server_uuid_sha256"]:
                raise RuntimeError("Validation and business connection refer to different servers")
            report["before"] = state(connection)
            connection.rollback()  # Release read metadata locks before ALTER.
            (folder/"before.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
            if args.mode == "upgrade":
                def allow_only_0092(conn, cursor, statement, parameters, context, executemany):
                    sql = " ".join(statement.split())
                    kind = sql.split(" ",1)[0].upper()
                    if kind in ("SELECT","SHOW","DESCRIBE","EXPLAIN","SET"):
                        return
                    alter = re.fullmatch(r"ALTER TABLE `?(\w+)`? MODIFY (?:COLUMN )?`?(\w+)`? LONGTEXT\b.*", sql, re.I)
                    version = re.match(r"UPDATE `?alembic_version`? SET\b", sql, re.I)
                    if not ((alter and alter[1] in FIELDS and alter[2] in FIELDS[alter[1]]) or version):
                        raise RuntimeError("Blocked a write outside the six payload columns and Alembic version")
                    report["executed_mutations"].append(sql)
                event.listen(connection, "before_cursor_execute", allow_only_0092)
                try:
                    connection.exec_driver_sql("SET SESSION lock_wait_timeout=15")
                    upgrade_verified(connection, REV92)
                finally:
                    event.remove(connection, "before_cursor_execute", allow_only_0092)
            report["after_identity"] = fingerprint(connection, args.expected_database)
            report["after"] = state(connection)
            connection.rollback()
            before, after = report["before"], report["after"]
            for key in ("data","column_contract","constraints","engines"):
                if before[key] != after[key]:
                    raise RuntimeError(f"Post-check mismatch: {key}")
            if args.mode == "upgrade":
                if report["after_identity"]["revisions"] != [REV92]:
                    raise RuntimeError("Post-upgrade revision mismatch")
                for table, names in FIELDS.items():
                    if not all(after["column_types"][table][name].lower().startswith("longtext") for name in names):
                        raise RuntimeError("Some target payload columns are not LONGTEXT")
            report.update(passed=True, all_seven_tables_data_equal=True, constraints_and_column_attributes_equal=True)
    except Exception as exc:
        report.update(error_type=type(exc).__name__)
        if isinstance(exc, RuntimeError):
            report["reason"] = str(exc)
    finally:
        engine.dispose()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("passed","mode","database","target_revision","error_type","reason") if key in report},ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
