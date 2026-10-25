"""Read-only MySQL baselines: schema, exact row hashes, keys and null counts.

capture saves hashes, NOT a restorable backup. verify fails closed on any
existing-row/column change. --allow-inserts permits new IDs only; even a NULL
fill in an old row fails verification and requires a separately reviewed plan.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys

import pymysql

KEY_FIELDS = {
    "universe_symbols": ("id", "symbol", "asset_type", "market", "region"),
    "symbols": ("id", "symbol", "asset_type", "market"),
    "universe_daily_bars": ("id", "universe_symbol_id", "trade_date", "open", "high", "low", "close", "source"),
    "daily_bars": ("id", "symbol_id", "trade_date", "open", "high", "low", "close", "source"),
    "stock_valuations": ("id", "symbol_id", "trade_date", "pe_ttm", "pb", "total_market_cap", "circulating_market_cap", "source"),
    "capital_flows": ("id", "symbol_id", "trade_date", "main_net_inflow", "main_net_inflow_pct", "super_large_net_inflow", "large_net_inflow", "medium_net_inflow", "small_net_inflow", "source"),
    "stock_financial_reports": ("id", "symbol_id", "report_period", "announcement_date", "report_type", "net_profit", "revenue", "net_profit_yoy", "revenue_yoy", "source"),
    "index_prices": ("id", "symbol", "trade_date", "open", "high", "low", "close", "source"),
    "stock_hot_rank_snapshots": ("id", "symbol", "trade_date", "hot_rank", "hot_rank_total", "hot_rank_pct", "source"),
    "etf_indicators": ("id", "symbol_id", "trade_date", "source"),
    "lhb_institution_trades": ("id", "symbol", "trade_date", "source"),
    "security_status_daily": ("id", "symbol_id", "trade_date"),
}


def stable_value(value):
    # Retain types: decimal precision, bytes, NULL, empty strings and zero differ.
    if isinstance(value, (date, datetime, Decimal)):
        return [type(value).__name__, str(value)]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    return [type(value).__name__, value]


def digest_rows(connection, table, columns, high_id, *, new=False, primary_keys=("id",)):
    digest = hashlib.sha256()
    nulls = dict.fromkeys(columns, 0)
    empty = dict.fromkeys(columns, 0)
    count = 0
    names = ",".join(f"`{column}`" for column in columns)
    operator = ">" if new else "<="
    with connection.cursor(pymysql.cursors.SSCursor) as cursor:
        if high_id is not None:
            cursor.execute(f"SELECT {names} FROM `{table}` WHERE `id` {operator} %s ORDER BY `id`", (high_id,))
        else:
            if not primary_keys:
                # Tables with no key must be small enough for a deterministic
                # full-row byte sort (SQL collation can equate different bytes).
                cursor.execute(f"SELECT {names} FROM `{table}`")
            else:
                order = ",".join(f"`{key}`" for key in primary_keys)
                cursor.execute(f"SELECT {names} FROM `{table}` ORDER BY {order}")
        unsorted = []
        for row in cursor:
            encoded = json.dumps([stable_value(v) for v in row], ensure_ascii=False,
                                 allow_nan=False, separators=(",", ":")).encode("utf-8")
            if high_id is None and not primary_keys:
                unsorted.append(encoded)
                if len(unsorted) > 100000:
                    raise ValueError(f"{table}: keyless table too large; explicit ordering review required")
            else:
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            count += 1
            for column, value in zip(columns, row):
                nulls[column] += value is None
                empty[column] += isinstance(value, str) and not value.strip()
        for encoded in sorted(unsorted):
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return {"rows": count, "sha256": digest.hexdigest(), "nulls": nulls, "empty": empty}


def inspect_table(connection, table, previous=None, allow_inserts=False):
    with connection.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(f"SHOW FULL COLUMNS FROM `{table}`")
        schema = cursor.fetchall()
        columns = [row["Field"] for row in schema]
        primary_keys = [row["Field"] for row in schema if row["Key"] == "PRI"]
        integer_id = primary_keys == ["id"] and next(row["Type"] for row in schema if row["Field"] == "id").lower().startswith(("int", "bigint", "smallint", "tinyint", "mediumint"))
        if not set(KEY_FIELDS.get(table, primary_keys)).issubset(columns):
            raise ValueError(f"{table}: declared key field absent; inspect schema before proceeding")
        if allow_inserts and (table not in KEY_FIELDS or not integer_id):
            raise ValueError("Only explicitly reviewed integer-key business tables may grow")
        maximum = "COALESCE(MAX(id),0)" if integer_id else "NULL"
        cursor.execute(f"SELECT COUNT(*) AS n, {maximum} AS max_id FROM `{table}`")
        totals = cursor.fetchone()
        cursor.execute(f"SHOW CREATE TABLE `{table}`")
        import re
        definition = re.sub(r" AUTO_INCREMENT=\d+", "", cursor.fetchone()["Create Table"])
    high_id = previous["max_id"] if previous else (int(totals["max_id"]) if integer_id else None)
    old = digest_rows(connection, table, columns, high_id, primary_keys=primary_keys)
    result = {"schema": schema, "max_id": high_id, "total": int(totals["n"]), "existing": old}
    result["definition"] = definition
    if previous:
        new = (digest_rows(connection, table, columns, high_id, new=True) if integer_id else
               {"rows": 0, "nulls": dict.fromkeys(columns, 0), "empty": dict.fromkeys(columns, 0)})
        result["new"] = new
        result["checks"] = {
            "schema_unchanged": schema == previous["schema"],
            "indexes_constraints_unchanged": definition == previous.get("definition", definition),
            "existing_rows_and_all_columns_unchanged": old == previous["existing"],
            "row_count": (result["total"] == previous["total"] + new["rows"]
                          if allow_inserts else result["total"] == previous["total"] and not new["rows"]),
            "new_key_fields_complete": all(new["nulls"][field] == new["empty"][field] == 0
                                            for field in KEY_FIELDS.get(table, primary_keys)),
        }
        result["passed"] = all(result["checks"].values())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "verify"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--tables", default=",".join(KEY_FIELDS))
    parser.add_argument("--all-tables", action="store_true", help="include every base table, including controls and schedules")
    parser.add_argument("--allow-inserts", action="store_true")
    parser.add_argument("--insert-tables", default="", help="explicit comma-separated tables permitted to grow")
    args = parser.parse_args()
    if args.mode == "capture" and args.state.exists():
        parser.error("baseline already exists; choose a new path instead of overwriting evidence")
    previous = json.loads(args.state.read_text(encoding="utf-8")) if args.mode == "verify" else None
    tables = list(previous["tables"]) if previous else args.tables.split(",")
    if not tables or (not args.all_tables and not (previous or {}).get("all_tables") and any(table not in KEY_FIELDS for table in tables)):
        parser.error("unknown table; inspect it and explicitly declare its key fields first")
    insert_tables = set(filter(None, args.insert_tables.split(",")))
    if args.allow_inserts != bool(insert_tables) or not insert_tables.issubset(tables):
        parser.error("--allow-inserts requires an explicit --insert-tables subset of the baseline")
    database = os.environ.get("AUDIT_DB_NAME", "gpfx_validation")
    if database != "gpfx_validation":
        parser.error("this validation runbook is restricted to gpfx_validation")
    connection = pymysql.connect(host=os.environ.get("AUDIT_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("AUDIT_DB_PORT", "3306")),
        user=os.environ.get("AUDIT_DB_USER", "root"), password=os.environ["AUDIT_DB_PASSWORD"],
        database=database, charset="utf8mb4", connect_timeout=10, read_timeout=300)
    evidence = {"database": database, "all_tables": args.all_tables or bool((previous or {}).get("all_tables")), "tables": {}}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            if evidence["all_tables"]:
                cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME")
                actual_tables = [row[0] for row in cursor.fetchall()]
                if previous and set(actual_tables) != set(tables):
                    raise ValueError("Database table set changed")
                tables = actual_tables
            import re
            if any(not re.fullmatch(r"[a-zA-Z0-9_]+", table) for table in tables):
                raise ValueError("Unexpected table identifier")
        for table in tables:
            evidence["tables"][table] = inspect_table(connection, table,
                previous["tables"][table] if previous else None, table in insert_tables)
            item = evidence["tables"][table]
            print(json.dumps({"table": table, "rows": item["total"],
                              "passed": item.get("passed"), "checks": item.get("checks")}), flush=True)
    finally:
        connection.rollback()
        connection.close()
    target = args.state if args.mode == "capture" else args.report
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = not previous or all(item["passed"] for item in evidence["tables"].values())
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
