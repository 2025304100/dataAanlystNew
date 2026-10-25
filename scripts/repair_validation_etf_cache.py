"""Back up and repair only the three reviewed ETF cache rows in gpfx_validation.

No application startup, ORM hooks, migrations, downloads, or task dispatch.
The projected verification state changes exactly the reviewed cache cells.
"""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if not __debug__:
    raise RuntimeError("Optimized Python disables safety assertions; refuse cache repair")
from scripts.backfill_validation_valuations import connect
from scripts.verify_data_management_integrity import stable_value

REVISION = "wps_0024_082_status_dt6"
TARGETS = {
    6549: ("560650", 971, "2026-07-01"),
    6604: ("560000", 1108, "2026-04-30"),
    7098: ("510300", 1280, "2026-10-23"),
}
CACHE_FIELDS = frozenset(("bar_count", "last_bar_date"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plain(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False, allow_nan=False))


def save_new(path, value):
    payload = (json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n").encode("utf-8")
    with Path(path).open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    assert Path(path).read_bytes() == payload, "Evidence readback mismatch"


def digest(rows, columns):
    h = hashlib.sha256()
    nulls = dict.fromkeys(columns, 0)
    empty = dict.fromkeys(columns, 0)
    for row in rows:
        values = [row[column] for column in columns]
        encoded = json.dumps([stable_value(v) for v in values], ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
        h.update(len(encoded).to_bytes(8, "big")); h.update(encoded)
        for column, value in zip(columns, values):
            nulls[column] += value is None
            empty[column] += isinstance(value, str) and not value.strip()
    return {"rows": len(rows), "sha256": h.hexdigest(), "nulls": nulls, "empty": empty}


def preflight(cur):
    cur.execute("SELECT DATABASE() AS name")
    assert cur.fetchone()["name"] == "gpfx_validation", "Wrong database"
    cur.execute("SELECT version_num FROM alembic_version")
    assert [r["version_num"] for r in cur.fetchall()] == [REVISION], "Revision changed"
    cur.execute("SELECT COUNT(*) AS n FROM async_tasks WHERE status IN ('running','queued')")
    assert cur.fetchone()["n"] == 0, "Tasks are active"
    cur.execute("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND EVENT_OBJECT_TABLE='universe_symbols' AND EVENT_MANIPULATION='UPDATE'")
    assert not cur.fetchall(), "UPDATE triggers need separate review"
    cur.execute("SHOW FULL COLUMNS FROM universe_symbols")
    columns = cur.fetchall()
    assert not any("on update" in r["Extra"].lower() for r in columns), "Automatic column update needs review"
    return [r["Field"] for r in columns]


def checked_cache(row, bars):
    identity = TARGETS.get(row["id"])
    assert identity and (row["symbol"], row["region"], row["asset_type"], row["market"]) == (
        identity[0], "cn", "etf", "sh"), "Unexpected ETF identity"
    assert bars and all(b["universe_symbol_id"] == row["id"] for b in bars)
    assert len({b["trade_date"] for b in bars}) == len(bars), "Duplicate business dates"
    assert all(b["id"] is not None and b["trade_date"] is not None
               and all(b[f] is not None for f in ("open", "high", "low", "close", "source")) for b in bars)
    result = {"bar_count": len(bars), "last_bar_date": max(b["trade_date"] for b in bars)}
    assert (result["bar_count"], str(result["last_bar_date"])) == identity[1:], "Reviewed bar totals changed"
    return result


def projected_state(baseline, rows, columns, targets):
    assert baseline["database"] == "gpfx_validation" and baseline["all_tables"]
    assert set(targets) == set(TARGETS), "Unexpected repair scope"
    old_digest = digest(rows, columns)
    assert old_digest == baseline["tables"]["universe_symbols"]["existing"], "Registry drift since baseline"
    projected = copy.deepcopy(rows)
    found = set()
    changes = []
    for row in projected:
        if row["id"] not in targets:
            continue
        fields = targets[row["id"]]
        assert set(fields) == CACHE_FIELDS, "Only reviewed cache fields may change"
        found.add(row["id"])
        for field, value in fields.items():
            assert value is not None, "Cache fields cannot be cleared"
            if row[field] != value:
                changes.append({"id": row["id"], "symbol": row["symbol"], "field": field,
                                "before": row[field], "after": value})
            row[field] = value
    assert found == set(TARGETS)
    expected = copy.deepcopy(baseline)
    expected["tables"]["universe_symbols"]["existing"] = digest(projected, columns)
    return expected, changes


def stage(baseline_path, folder):
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert not (folder / "cache-backup.json").exists(), "Do not overwrite backup"
    with connect() as db, db.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        columns = preflight(cur)
        cur.execute("SELECT * FROM universe_symbols ORDER BY id")
        registry = cur.fetchall()
        cur.execute("SHOW FULL COLUMNS FROM universe_daily_bars")
        bar_columns = [r["Field"] for r in cur.fetchall()]
        saved, targets = [], {}
        for row in registry:
            if row["id"] not in TARGETS:
                continue
            cur.execute("SELECT * FROM universe_daily_bars WHERE universe_symbol_id=%s ORDER BY trade_date,id", (row["id"],))
            bars = cur.fetchall()
            targets[row["id"]] = checked_cache(row, bars)
            saved.append({"before_row": row, "verified_cache": targets[row["id"]], "bars_digest": digest(bars, bar_columns)})
        db.rollback()
    expected, changes = projected_state(baseline, registry, columns, targets)
    assert len(changes) == 4, "Reviewed repair must change exactly four cells"
    folder.mkdir(parents=True, exist_ok=True)
    save_new(folder / "expected-after-all.json", expected)
    backup = {"database": "gpfx_validation", "revision": REVISION,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "baseline_sha256": sha(baseline_path), "expected_state_sha256": sha(folder / "expected-after-all.json"),
              "allowed_fields": sorted(CACHE_FIELDS), "columns": columns, "bar_columns": bar_columns,
              "rows": saved, "changes": changes}
    save_new(folder / "cache-backup.json", backup)
    restore = ["-- Manual recovery only; compare backup/receipt before considering COMMIT.",
               "USE gpfx_validation;", "START TRANSACTION;"]
    for item in saved:
        old = item["before_row"]; new = item["verified_cache"]
        restore.append(f"UPDATE universe_symbols SET bar_count={int(old['bar_count'])},last_bar_date='{old['last_bar_date']}' "
                       f"WHERE id={int(old['id'])} AND symbol='{old['symbol']}' AND bar_count={int(new['bar_count'])} "
                       f"AND last_bar_date='{new['last_bar_date']}';")
        restore.append("SELECT ROW_COUNT() AS restored_rows; -- require exactly 1")
    restore.append("ROLLBACK; -- Replace with COMMIT only after explicitly reviewing all three recovery rows.")
    with (folder / "restore-cache.sql").open("x", encoding="utf-8") as f:
        f.write("\n".join(restore) + "\n"); f.flush(); os.fsync(f.fileno())
    return {"backup_sha256": sha(folder / "cache-backup.json"), "changes": plain(changes)}


def apply_backup(backup_path, expected_hash):
    assert sha(backup_path) == expected_hash, "Backup checksum mismatch"
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup["database"] == "gpfx_validation" and backup["revision"] == REVISION
    assert set(backup["allowed_fields"]) == CACHE_FIELDS
    assert {r["before_row"]["id"] for r in backup["rows"]} == set(TARGETS)
    assert sha(backup_path.parent / "expected-after-all.json") == backup["expected_state_sha256"]
    assert not (backup_path.parent / "apply-receipt.json").exists(), "Repair already has a receipt"
    result_rows = []
    with connect() as db:
        try:
            with db.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("START TRANSACTION")
                columns = preflight(cur)
                assert columns == backup["columns"]
                cur.execute("SELECT * FROM universe_symbols WHERE id IN (6549,6604,7098) ORDER BY id FOR UPDATE")
                current = {r["id"]: r for r in cur.fetchall()}
                for item in backup["rows"]:
                    old = item["before_row"]; symbol_id = old["id"]
                    assert plain(current[symbol_id]) == old, "Old registry row changed; no overwrite"
                    cur.execute("SELECT * FROM universe_daily_bars WHERE universe_symbol_id=%s ORDER BY trade_date,id FOR UPDATE", (symbol_id,))
                    bars = cur.fetchall()
                    assert digest(bars, backup["bar_columns"]) == item["bars_digest"], "Source bars changed; no overwrite"
                    verified = checked_cache(current[symbol_id], bars)
                    assert plain(verified) == item["verified_cache"], "Recomputed cache differs"
                    cur.execute("UPDATE universe_symbols SET bar_count=%s,last_bar_date=%s WHERE id=%s "
                                "AND bar_count <=> %s AND last_bar_date <=> %s",
                                (verified["bar_count"], verified["last_bar_date"], symbol_id, old["bar_count"], old["last_bar_date"]))
                    assert cur.rowcount == 1, "Exactly one cache row must change"
                    cur.execute("SELECT * FROM universe_symbols WHERE id=%s", (symbol_id,))
                    after = cur.fetchone()
                    expected = {**old, **plain(verified)}
                    assert plain(after) == expected, "An unapproved column changed; roll back"
                    result_rows.append({"id": symbol_id, "symbol": old["symbol"],
                                        "before_row": old, "after_row": plain(after), "bars_digest": item["bars_digest"]})
            db.commit()
        except BaseException:
            db.rollback()
            raise
    receipt = {"committed": True, "database": "gpfx_validation", "backup_sha256": expected_hash,
               "committed_at": datetime.now(timezone.utc).isoformat(), "rows_updated": 3,
               "cells_changed": 4, "allowed_fields": sorted(CACHE_FIELDS), "rows": result_rows}
    save_new(backup_path.parent / "apply-receipt.json", receipt)
    return {"committed": True, "rows_updated": 3, "cells_changed": 4}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("stage"); p.add_argument("--baseline", type=Path, required=True); p.add_argument("--folder", type=Path, required=True)
    p = sub.add_parser("apply"); p.add_argument("--backup", type=Path, required=True); p.add_argument("--sha256", required=True)
    args = parser.parse_args()
    result = stage(args.baseline, args.folder) if args.mode == "stage" else apply_backup(args.backup, args.sha256)
    print(json.dumps(result, ensure_ascii=False, default=str))
