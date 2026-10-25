"""Read-only, full-row migration comparison. Never opens the app's default DB.

Use a stopped-writer clone first. REVIEW_DATABASE_URL must be explicit. Capture
all pre-existing columns, including columns no longer mapped by the ORM. New
columns are deliberately excluded from data equality and need separate schema
and business validation. No-PK tables fail closed rather than silently skipping.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


def canonical(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, datetime):
        return {"datetime": value.isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, time):
        return {"time": value.isoformat(timespec="microseconds")}
    if isinstance(value, timedelta):
        return {"timedelta": [value.days, value.seconds, value.microseconds]}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, float):
        return {"float_hex": value.hex()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": base64.b64encode(bytes(value)).decode("ascii")}
    raise TypeError(f"Unsupported value type: {type(value).__name__}")


def digest_table(conn, table, columns, keys):
    quote = conn.dialect.identifier_preparer.quote_identifier
    query = "SELECT " + ", ".join(map(quote, columns))
    query += " FROM " + quote(table)
    query += " ORDER BY " + ", ".join(map(quote, keys))
    digest = hashlib.sha256()
    nulls = {column: 0 for column in columns}
    count = 0
    result = conn.execution_options(stream_results=True).execute(text(query))
    try:
        for row in result:
            encoded = json.dumps([canonical(value) for value in row],
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
            for column, value in zip(columns, row):
                if value is None:
                    nulls[column] += 1
    finally:
        result.close()
    return {"rows": count, "sha256_all_old_columns": digest.hexdigest(), "nulls": nulls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "verify"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-database", required=True,
                        help="Exact database name (SQLite: exact URL database path).")
    parser.add_argument("--report", help="Optional JSON verification report path.")
    args = parser.parse_args()
    db_url = os.environ.get("REVIEW_DATABASE_URL")
    if not db_url:
        parser.error("Set REVIEW_DATABASE_URL explicitly; no config fallback is allowed.")
    engine = create_engine(db_url)
    manifest_path = Path(args.manifest)
    try:
        with engine.connect() as conn:
            if conn.dialect.name == "mysql":
                conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
                database = conn.exec_driver_sql("SELECT DATABASE()").scalar_one()
            elif conn.dialect.name == "sqlite":
                conn.exec_driver_sql("PRAGMA query_only=ON")
                database = engine.url.database
            else:
                raise RuntimeError("Only MySQL and isolated SQLite are supported.")
            if database != args.expected_database:
                raise RuntimeError("Database identity mismatch; no table content read.")
            inspector = inspect(conn)
            tables = set(inspector.get_table_names()) - {"alembic_version"}
            if args.mode == "capture":
                if manifest_path.exists():
                    raise RuntimeError("Refusing to overwrite baseline manifest.")
                manifest = {"database": database, "dialect": conn.dialect.name,
                            "excluded": ["alembic_version"], "tables": {}}
                for table in sorted(tables):
                    columns = [item["name"] for item in inspector.get_columns(table)]
                    keys = inspector.get_pk_constraint(table).get("constrained_columns") or []
                    if not keys:
                        raise RuntimeError(f"{table}: no primary key; explicit review required.")
                    manifest["tables"][table] = {"columns": columns, "order_by": keys,
                        **digest_table(conn, table, columns, keys)}
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"CAPTURED {len(manifest['tables'])} tables. Writers must remain stopped.")
                return 0
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["database"] != database or manifest["dialect"] != conn.dialect.name:
                raise RuntimeError("Manifest identity mismatch.")
            failures = []
            for table, before in manifest["tables"].items():
                if table not in tables:
                    failures.append({"table": table, "error": "table_missing"})
                    continue
                present = {item["name"] for item in inspector.get_columns(table)}
                missing = set(before["columns"]) - present
                if missing:
                    failures.append({"table": table, "error": "columns_missing", "columns": sorted(missing)})
                    continue
                after = digest_table(conn, table, before["columns"], before["order_by"])
                if any(before[key] != after[key] for key in after):
                    failures.append({"table": table, "error": "data_changed",
                                     "before": {key: before[key] for key in after}, "after": after})
            report = {"passed": not failures, "compared_tables": len(manifest["tables"]),
                      "new_tables_require_schema_review": sorted(tables - set(manifest["tables"])),
                      "failures": failures}
            if args.report:
                Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if failures else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
