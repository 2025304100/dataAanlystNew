"""Freeze bounded ETF/fund-flow gaps, stage source evidence, then INSERT only.

This runbook never calls application initialization, task dispatch or upserts.
Existing rows (including registry caches) are immutable. Unverified source
adjustment/units/anchors remain explicit failures, never a raw-price fallback.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.backfill_validation_valuations import connect

ETF_START, ETF_END = date(2026, 10, 20), date(2026, 10, 23)
CF_START, CF_END = date(2026, 10, 14), date(2026, 10, 16)
ETF_FIELDS = ("open", "high", "low", "close", "volume", "amount", "turnover_rate")
CF_FIELDS = ("main_net_inflow", "main_net_inflow_pct", "super_large_net_inflow",
             "large_net_inflow", "medium_net_inflow", "small_net_inflow")


def save(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False, default=str)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with connect() as db, db.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        cursor.execute("SELECT `date` FROM trade_calendar WHERE is_trading_day=1 AND `date` BETWEEN %s AND %s", (ETF_START, ETF_END))
        days = sorted(str(row["date"]) for row in cursor.fetchall())
        if len(days) != 4:
            raise ValueError("ETF calendar differs from reviewed four-session scope")
        cursor.execute("SELECT id,symbol,market,listed_at FROM universe_symbols WHERE asset_type='etf' AND region='cn' ORDER BY id")
        symbols = cursor.fetchall()
        for symbol in symbols:
            cursor.execute("SELECT * FROM universe_daily_bars WHERE universe_symbol_id=%s AND trade_date BETWEEN '2026-10-06' AND %s ORDER BY trade_date", (symbol["id"], ETF_END))
            rows = cursor.fetchall()
            present = {str(row["trade_date"]) for row in rows}
            symbol["missing"] = [day for day in days if day not in present]
            symbol["anchors"] = [row for row in rows if row["trade_date"] < ETF_START][-7:]
            symbol["existing_target"] = [row for row in rows if row["trade_date"] >= ETF_START]
        gaps = json.loads((ROOT / "tmp/data-management-audit-20261025/capital-gap-selection.json").read_text(encoding="utf-8"))["gaps"]
        capital = {}
        for row in gaps:
            if not CF_START <= date.fromisoformat(row["trade_date"]) <= CF_END:
                raise ValueError("Capital selection date mismatch")
            cursor.execute("SELECT id,symbol,market,asset_type FROM symbols WHERE id=%s", (row["symbol_id"],))
            symbol = cursor.fetchone()
            if not symbol or symbol["symbol"] != row["symbol"] or symbol["asset_type"] != "stock":
                raise ValueError("Capital selection identity mismatch")
            cursor.execute("SELECT id FROM capital_flows WHERE symbol_id=%s AND trade_date=%s", (symbol["id"], row["trade_date"]))
            if cursor.fetchone():
                continue
            cursor.execute("SELECT is_trading_day FROM trade_calendar WHERE `date`=%s", (row["trade_date"],))
            if (cursor.fetchone() or {}).get("is_trading_day") != 1:
                raise ValueError("Capital selection contains an unconfirmed trading date")
            capital.setdefault(symbol["symbol"], {**symbol, "missing": []})["missing"].append(row["trade_date"])
    scope = {"database": "gpfx_validation", "etf_start": str(ETF_START), "etf_end": str(ETF_END),
             "sessions": days, "etfs": symbols, "capital": list(capital.values()),
             "created_at": datetime.now(timezone.utc).isoformat()}
    save(directory / "scope.json", scope)
    print(json.dumps({"etfs": len(symbols), "etf_candidates": sum(len(s["missing"]) for s in symbols),
                      "capital_candidates": sum(len(s["missing"]) for s in capital.values()),
                      "scope_sha256": digest(directory / "scope.json")}), flush=True)


def finite(row, fields, *, nonnegative=False):
    values = [float(row[field]) for field in fields]
    if not all(math.isfinite(value) for value in values) or (nonnegative and min(values) < 0):
        raise ValueError("Missing/nonfinite/negative required source field")
    return dict(zip(fields, values))


def validate_etf_rows(symbol, frame):
    response = frame.attrs["history_response"]
    if response.get("actual_adjustment") != "qfq":
        raise ValueError("ETF qfq evidence required")
    reference = {row["trade_date"]: row for row in frame.attrs["etf_basis_reference"]["rows"]}
    anchors = symbol["anchors"]
    if len(anchors) < 3:
        raise ValueError("Fewer than three existing reference sessions; no continuity claim")
    # Legacy source labels remain untouched/unknown. We only establish direct
    # numerical continuity against all available (up to seven) old observations;
    # we do not relabel or certify the rest of their historical series.
    maxima = {field: 0. for field in ETF_FIELDS[:-1]}
    for old in anchors:
        new = reference.get(old["trade_date"])
        if new is None:
            raise ValueError("Source omitted an existing reference session")
        for field in maxima:
            if old[field] is None or new[field] is None:
                raise ValueError("Existing reference price/quantity incomplete")
            difference = abs(float(old[field]) - float(new[field]))
            tolerance = 50.000001 if field == "volume" else .010001 if field == "amount" else .000500001
            if not math.isfinite(difference) or difference > tolerance:
                raise ValueError(f"Reference mismatch: {old['trade_date']} {field}")
            maxima[field] = max(maxima[field], difference)
    records = []
    for raw in frame.to_dict(orient="records"):
        day = raw["trade_date"].date().isoformat()
        if day not in symbol["missing"]:
            continue
        fields = finite(raw, ETF_FIELDS, nonnegative=True)
        if min(fields[k] for k in ETF_FIELDS[:4]) <= 0:
            raise ValueError("ETF key price must be positive")
        records.append({"universe_symbol_id": symbol["id"], "symbol": symbol["symbol"],
                        "trade_date": day, **fields, "source": "tencent:cn-etf:v1:qfq"})
    return records, {"anchors": len(anchors), "max_differences": maxima,
                     "legacy_history_relabelled": False}


def stage_etf(symbol):
    from app.services.market_data_sources.sources.tencent_etf_history import fetch_tencent_etf_history
    frame = fetch_tencent_etf_history(symbol=SimpleNamespace(**symbol, asset_type="etf"),
        start_date=ETF_START, end_date=ETF_END, adjust="qfq")
    records, comparison = validate_etf_rows(symbol, frame)
    return {"records": records, "comparison": comparison, "evidence": frame.attrs,
            "missing_after_source": sorted(set(symbol["missing"]) - {row["trade_date"] for row in records})}


def stage_capital(symbol, archived=None):
    from app.services.capital_flow_history_provider import fetch_capital_flow_history, parse_capital_flow_payload
    if archived is None:
        frame = fetch_capital_flow_history(stock=symbol["symbol"], market=symbol["market"])
        receipt = frame.attrs["source_receipt"]
    else:
        receipt = archived
        frame = parse_capital_flow_payload(receipt["payload"], stock=symbol["symbol"], market=symbol["market"])
    mapping = dict(zip(CF_FIELDS, ("主力净流入-净额", "主力净流入-净占比", "超大单净流入-净额",
                                 "大单净流入-净额", "中单净流入-净额", "小单净流入-净额")))
    records = []
    for raw in frame.to_dict(orient="records"):
        day = str(raw["日期"])
        if day not in symbol["missing"]:
            continue
        fields = finite({field: raw[source] for field, source in mapping.items()}, CF_FIELDS)
        records.append({"symbol_id": symbol["id"], "symbol": symbol["symbol"], "trade_date": day,
                        **fields, "source": "eastmoney:flow_backfill", "raw": {**raw, "_history_complete": True}})
    return {"records": records, "evidence": receipt,
            "missing_after_source": sorted(set(symbol["missing"]) - {row["trade_date"] for row in records})}


def stage(directory, dataset, workers):
    scope_file = directory / "scope.json"
    scope = json.loads(scope_file.read_text(encoding="utf-8"))
    source = scope["etfs" if dataset == "etf" else "capital"]
    target = directory / dataset
    target.mkdir(exist_ok=True)
    scope_hash = digest(scope_file)
    def one(symbol):
        path = target / (symbol["symbol"] + ".json")
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["scope_sha256"] != scope_hash:
                raise ValueError("Resume scope hash mismatch")
            return saved
        result = {"symbol": symbol["symbol"], "scope_sha256": scope_hash}
        try:
            result.update((stage_etf if dataset == "etf" else stage_capital)(symbol))
            result["status"] = "verified" if result["records"] else "no_selected_rows"
        except Exception as error:
            result.update(status="blocked", records=[], error=str(error),
                          cause=str(error.__cause__) if error.__cause__ else None,
                          missing_after_source=symbol["missing"])
        save(path, result)
        time.sleep(.15)
        return result
    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        futures = [pool.submit(one, symbol) for symbol in source if symbol["missing"]]
        for future in as_completed(futures):
            results.append(future.result())
            if len(results) % 50 == 0 or len(results) == len(futures):
                print(json.dumps({"dataset": dataset, "processed": len(results), "total": len(futures),
                    "verified": sum(r["status"] == "verified" for r in results),
                    "rows": sum(len(r["records"]) for r in results)}), flush=True)
    manifest = {"database": "gpfx_validation", "dataset": dataset, "scope_sha256": scope_hash,
                "records": [row for result in results for row in result["records"]],
                "files": {p.name: digest(p) for p in sorted(target.glob('*.json'))},
                "blocked": [{k: result.get(k) for k in ("symbol", "status", "error", "cause", "missing_after_source")}
                            for result in results if result["missing_after_source"]]}
    path = directory / (dataset + "-manifest.json")
    save(path, manifest)
    print(json.dumps({"manifest": str(path), "sha256": digest(path), "rows": len(manifest["records"]),
                      "remaining_symbols": len(manifest["blocked"])}), flush=True)


def apply(manifest_path, expected_hash, baseline):
    if digest(manifest_path) != expected_hash:
        raise ValueError("Manifest digest mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    before = json.loads(baseline.read_text(encoding="utf-8"))
    directory = manifest_path.parent
    if digest(directory / "scope.json") != manifest["scope_sha256"]:
        raise ValueError("Scope digest mismatch")
    scope = json.loads((directory / "scope.json").read_text(encoding="utf-8"))
    if any(item["database"] != "gpfx_validation" for item in (manifest, before, scope)):
        raise ValueError("Wrong database")
    dataset = manifest["dataset"]
    if dataset not in {"etf", "capital"}:
        raise ValueError("Unreviewed dataset")
    for name, checksum in manifest["files"].items():
        if Path(name).name != name or digest(directory / dataset / name) != checksum:
            raise ValueError("Source receipt checksum mismatch")
    table, id_column, fields = (("universe_daily_bars", "universe_symbol_id", ETF_FIELDS) if dataset == "etf"
                                else ("capital_flows", "symbol_id", CF_FIELDS))
    symbols = {row["symbol"]: row for row in scope["etfs" if dataset == "etf" else "capital"]}
    records, seen = manifest["records"], set()
    for row in records:
        symbol = symbols.get(row["symbol"])
        if symbol is None or row[id_column] != symbol["id"] or row["trade_date"] not in symbol["missing"]:
            raise ValueError("Row outside frozen missing-key scope")
        key = (row[id_column], row["trade_date"])
        if key in seen:
            raise ValueError("Duplicate staged key")
        seen.add(key)
        finite(row, fields, nonnegative=dataset == "etf")
        if dataset == "etf" and (min(row[k] for k in ETF_FIELDS[:4]) <= 0
                or row["low"] > min(row["open"], row["close"])
                or row["high"] < max(row["open"], row["close"])):
            raise ValueError("Invalid staged OHLC")
    original = before["tables"][table]
    with connect() as db:
        try:
            with db.cursor() as cursor:
                cursor.execute("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
                cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
                if cursor.fetchone()["ENGINE"].lower() != "innodb":
                    raise ValueError("Transactional storage required")
                cursor.execute("SELECT COUNT(*) n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=%s", (table,))
                if cursor.fetchone()["n"]:
                    raise ValueError("Unreviewed write triggers")
                cursor.execute("SELECT COUNT(*) n FROM async_tasks WHERE status IN ('running','queued')")
                if cursor.fetchone()["n"]:
                    raise ValueError("Concurrent task writers")
                cursor.execute(f"SELECT COUNT(*) n,COALESCE(MAX(id),0) max_id FROM `{table}`")
                totals = cursor.fetchone()
                if (totals["n"], totals["max_id"]) != (original["total"], original["max_id"]):
                    raise ValueError("Baseline changed; never replay a stale manifest")
                values = []
                stamp = datetime.now(timezone.utc).replace(tzinfo=None)
                for row in sorted(records, key=lambda r: (r[id_column], r['trade_date'])):
                    registry = "universe_symbols" if dataset == "etf" else "symbols"
                    cursor.execute(f"SELECT symbol,asset_type,market FROM {registry} WHERE id=%s", (row[id_column],))
                    actual = cursor.fetchone()
                    if actual != {"symbol": row["symbol"], "asset_type": "etf" if dataset == "etf" else "stock", "market": symbols[row['symbol']]['market']}:
                        raise ValueError("Registry identity changed")
                    source = "tencent:cn-etf:v1:qfq" if dataset == "etf" else "eastmoney:flow_backfill"
                    value = [row[id_column], row["trade_date"], *(row[field] for field in fields), source, stamp]
                    if dataset == "capital":
                        value.append(json.dumps(row["raw"], ensure_ascii=False, allow_nan=False))
                    values.append(tuple(value))
                columns = [id_column, "trade_date", *fields, "source", "created_at"]
                if dataset == "capital":
                    columns.append("raw_json")
                if values:
                    cursor.executemany(f"INSERT INTO `{table}` (" + ",".join(f"`{c}`" for c in columns) + ") VALUES (" + ",".join(["%s"]*len(columns)) + ")", values)
                    if cursor.rowcount != len(values):
                        raise ValueError("Insert row accounting failed")
                cursor.execute(f"SELECT COUNT(*) n FROM `{table}`")
                if cursor.fetchone()["n"] != original["total"] + len(values):
                    raise ValueError("Total row accounting failed")
                cursor.execute(f"SELECT {','.join(f'`{c}`' for c in columns)} FROM `{table}` WHERE id>%s", (original["max_id"],))
                inserted = cursor.fetchall()
                wanted = {(r[id_column], r['trade_date']): r for r in records}
                if {(r[id_column], str(r['trade_date'])) for r in inserted} != set(wanted):
                    raise ValueError("Inserted key set differs from manifest")
                for row in inserted:
                    expected = wanted[row[id_column], str(row['trade_date'])]
                    if any(row[f] != expected[f] for f in fields):
                        raise ValueError("Stored numeric fields differ from source")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    receipt = {"database": "gpfx_validation", "table": table, "inserted": len(records),
               "before": original["total"], "after": original["total"] + len(records),
               "old_max_id": original["max_id"], "manifest_sha256": expected_hash,
               "exact_new_keys_and_values_verified_before_commit": True,
               "original_rows_updated": 0, "committed_at": datetime.now(timezone.utc).isoformat()}
    save(manifest_path.with_suffix(".receipt.json"), receipt)
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "stage", "apply"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--dataset", choices=("etf", "capital"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.mode == "freeze":
        freeze(args.directory)
    elif args.mode == "stage":
        stage(args.directory, args.dataset, args.workers)
    else:
        apply(args.manifest, args.sha256, args.baseline)
