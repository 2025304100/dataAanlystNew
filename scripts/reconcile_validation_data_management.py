"""Read-only ETF identity/cache/source reconciliation; never fetch or repair data.

Uses the frozen previous rollout scope and already archived, hash-verified
responses. Source differences are observations, not instructions to overwrite.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.backfill_validation_valuations import connect
from app.services.market_data_sources.sources.tencent_etf_history import parse_etf_response


def read_receipt(directory, name):
    receipt = json.loads((directory / f"{name}.receipt.json").read_text(encoding="utf-8"))
    assert receipt["raw_response"] == f"{name}.response.json"
    payload = (directory / receipt["raw_response"]).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == receipt["response_sha256"], "Archive checksum mismatch"
    params = receipt["parameters"]
    frame = parse_etf_response(payload, prefixed=params["code"], start=date.fromisoformat(params["fromDate"]),
                               end=date.fromisoformat(params["toDate"]), adjustment=params["fqtype"])
    return receipt, frame


def reconcile(scope_path):
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    assert scope["database"] == "gpfx_validation"
    symbols = {s["symbol"]: s for s in scope["etfs"]}
    with connect() as db, db.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        cur.execute("SELECT DATABASE() AS name")
        assert cur.fetchone()["name"] == "gpfx_validation"
        cur.execute("""SELECT u.id,u.symbol,u.market,u.listed_at,u.last_bar_date,u.bar_count,u.last_synced_at,
            COUNT(b.id) AS actual_bar_count,MAX(b.trade_date) AS actual_last_bar_date
            FROM universe_symbols u LEFT JOIN universe_daily_bars b ON b.universe_symbol_id=u.id
            WHERE u.region='cn' AND u.asset_type='etf'
            GROUP BY u.id,u.symbol,u.market,u.listed_at,u.last_bar_date,u.bar_count,u.last_synced_at ORDER BY u.id""")
        registry = cur.fetchall()
        assert {(r["id"], r["symbol"], r["market"]) for r in registry} == {
            (s["id"], s["symbol"], s["market"]) for s in symbols.values()}, "Frozen ETF identity/scope changed"
        cur.execute("""SELECT u.symbol,b.* FROM universe_daily_bars b JOIN universe_symbols u ON u.id=b.universe_symbol_id
            WHERE u.region='cn' AND u.asset_type='etf' AND b.trade_date BETWEEN %s AND %s ORDER BY u.id,b.trade_date""",
            ("2026-10-06", scope["etf_end"]))
        observed = cur.fetchall()
        cur.execute("""SELECT b.source,COUNT(*) AS rows_count FROM universe_daily_bars b
            JOIN universe_symbols u ON u.id=b.universe_symbol_id WHERE u.region='cn' AND u.asset_type='etf'
            GROUP BY b.source ORDER BY b.source""")
        sources = cur.fetchall()
        cur.execute("SHOW FULL COLUMNS FROM universe_daily_bars")
        columns = [{k: r[k] for k in ("Field", "Type", "Null")} for r in cur.fetchall()]
        db.rollback()
    cache_diffs = []
    for row in registry:
        reasons = []
        if row["bar_count"] != row["actual_bar_count"]: reasons.append("bar_count")
        if row["last_bar_date"] != row["actual_last_bar_date"]: reasons.append("last_bar_date")
        if reasons: cache_diffs.append({**row, "different_fields": reasons})
    current = {(r["symbol"], str(r["trade_date"])): r for r in observed}
    frozen_changes = []
    for symbol in symbols.values():
        for old in symbol["anchors"] + symbol["existing_target"]:
            actual = current.get((symbol["symbol"], old["trade_date"]))
            changed = [k for k, v in old.items() if actual is None or str(actual[k]) != str(v)]
            if changed: frozen_changes.append({"symbol": symbol["symbol"], "date": old["trade_date"], "fields": changed})
    archives = {}
    rejected = Counter()
    for path in sorted((ROOT / "tmp/market-history-etf").glob("*/qfq.receipt.json")):
        metadata = json.loads(path.read_text(encoding="utf-8")); params = metadata["parameters"]
        code = params["code"][2:]
        if code not in symbols or params["fromDate"] != "2026-10-06" or params["toDate"] != scope["etf_end"]:
            continue
        try:
            qfq, frame = read_receipt(path.parent, "qfq")
            raw, raw_frame = read_receipt(path.parent, "raw")
            assert params["code"] == symbols[code]["market"] + code
            assert raw["parameters"] == {**params, "fqtype": ""}
            assert frame.trade_date.equals(raw_frame.trade_date)
            assert frame[["volume", "amount"]].equals(raw_frame[["volume", "amount"]])
        except (ValueError, AssertionError, FileNotFoundError) as exc:
            rejected[type(exc).__name__ + ":" + str(exc)] += 1
            continue
        if code not in archives or qfq["fetched_at"] > archives[code][0]["fetched_at"]:
            archives[code] = (qfq, frame, path.parent.name)
    differences = []
    compared = set()
    limits = {"open": .000500001, "high": .000500001, "low": .000500001,
              "close": .000500001, "volume": 50.000001, "amount": .010001}
    for code, (receipt, frame, receipt_id) in archives.items():
        anchors = {r["trade_date"] for r in symbols[code]["anchors"]}
        for new in frame.to_dict("records"):
            day = new["trade_date"].date().isoformat()
            if day not in anchors: continue
            old = current.get((code, day))
            if old is None: continue
            compared.add((code, day))
            for field, tolerance in limits.items():
                difference = None if old[field] is None else float(new[field]) - float(old[field])
                if difference is None or abs(difference) > tolerance:
                    differences.append({"symbol": code, "trade_date": day, "field": field,
                        "old_value": old[field], "observed_source_value": new[field], "difference": difference,
                        "source_to_old_ratio": float(new[field]) / float(old[field]) if old[field] else None,
                        "old_source": old["source"], "receipt_id": receipt_id,
                        "response_sha256": receipt["response_sha256"], "reviewed_tolerance": tolerance})
    return {"database": "gpfx_validation", "read_only": True,
        "scope_sha256": hashlib.sha256(scope_path.read_bytes()).hexdigest(),
        "registry": registry, "listing_date_missing": [r["symbol"] for r in registry if r["listed_at"] is None],
        "cache_differences": cache_diffs, "frozen_row_changes": frozen_changes,
        "stored_source_counts": sources, "stored_columns": columns,
        "paired_source_symbols": len(archives), "compared_anchor_points": len(compared),
        "source_archive_rejections": dict(rejected), "source_field_differences": differences,
        "source_field_difference_counts": dict(Counter(r["field"] for r in differences)),
        "source_difference_symbols": len({r["symbol"] for r in differences}),
        "missing_evidence": {
            "listing": ["exchange-confirmed listing date", "delisting/termination date if applicable", "dated trading-status evidence"],
            "cache": ["separately reviewed cache-field mutation whitelist; no old fields changed in this run"],
            "history": ["original historical import response", "original units and conversion rules", "adjustment mode and dated corporate actions", "original numeric precision before storage"],
        }, "interpretation": "Differences do not determine which observation is correct. No old values were overwritten."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists(): parser.error("Do not overwrite existing evidence")
    report = reconcile(args.scope)
    with args.report.open("x", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2, default=str, allow_nan=False)
    print(json.dumps({"symbols": len(report["registry"]), "listing_date_missing": len(report["listing_date_missing"]),
        "cache_mismatches": len(report["cache_differences"]), "frozen_row_changes": len(report["frozen_row_changes"]),
        "paired_source_symbols": report["paired_source_symbols"], "compared_anchor_points": report["compared_anchor_points"],
        "source_field_difference_counts": report["source_field_difference_counts"]}))
