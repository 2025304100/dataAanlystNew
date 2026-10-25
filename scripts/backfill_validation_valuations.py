"""Stage real dated valuations, then insert only missing business keys.

Restricted to gpfx_validation. Never UPDATE/REPLACE/DELETE an existing row.
Source response, exact scope and SHA256 are saved for independent review.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import pymysql
import requests

SOURCE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE_COLUMNS = ("PE_TTM", "PB_MRQ", "TOTAL_MARKET_CAP", "NOTLIMITED_MARKETCAP_A")
TARGET_COLUMNS = ("pe_ttm", "pb", "total_market_cap", "circulating_market_cap")


def connect():
    if os.environ.get("AUDIT_DB_NAME", "gpfx_validation") != "gpfx_validation":
        raise ValueError("Only gpfx_validation is authorized by this runbook")
    return pymysql.connect(host=os.environ.get("AUDIT_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("AUDIT_DB_PORT", "3306")),
        user=os.environ.get("AUDIT_DB_USER", "root"), password=os.environ["AUDIT_DB_PASSWORD"],
        database="gpfx_validation", charset="utf8mb4", read_timeout=60,
        cursorclass=pymysql.cursors.DictCursor)


def stage(start, end, target):
    if target.exists():
        raise ValueError("Do not overwrite a staged evidence file")
    if start > end or (end - start).days > 31 or end >= date.today():
        raise ValueError("Require a closed historical range of at most 32 calendar days")
    with connect() as db:
        with db.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("""SELECT s.id,s.symbol FROM symbols s JOIN universe_symbols u
                ON u.symbol=s.symbol AND u.region='cn' AND u.asset_type='stock'
                WHERE s.asset_type='stock' ORDER BY s.id""")
            symbols = {row["symbol"]: row["id"] for row in cursor.fetchall()}
            if not symbols or any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in symbols):
                raise ValueError("Invalid/empty stock registry; no symbol inference is allowed")
            cursor.execute("SELECT symbol_id,trade_date FROM stock_valuations WHERE trade_date BETWEEN %s AND %s", (start, end))
            existing = {(row["symbol_id"], row["trade_date"].isoformat()) for row in cursor.fetchall()}
            cursor.execute("SELECT `date` FROM trade_calendar WHERE is_trading_day=1 AND `date` BETWEEN %s AND %s", (start, end))
            sessions = {row["date"].isoformat() for row in cursor.fetchall()}
        db.rollback()
    codes = list(symbols)
    if not sessions:
        raise ValueError("No confirmed trading sessions in the selected range")
    records, responses, seen = [], [], set()
    http = requests.Session()
    # Same direct-network behavior as the application's _proxy_bypass.
    http.trust_env = False
    for offset in range(0, len(codes), 200):
        batch = codes[offset:offset + 200]
        params = {"reportName": "RPT_VALUEANALYSIS_DET", "columns": "SECURITY_CODE,TRADE_DATE," + ",".join(SOURCE_COLUMNS),
            "filter": f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')(SECURITY_CODE in (" + ",".join(f'"{code}"' for code in batch) + "))",
            "pageSize": "1000", "sortColumns": "TRADE_DATE,SECURITY_CODE", "sortTypes": "1,1", "source": "WEB", "client": "WEB"}
        page, pages, expected, received = 1, 1, None, 0
        while page <= pages:
            response = http.get(SOURCE_URL, params={**params, "pageNumber": str(page)}, timeout=(5, 20))
            response.raise_for_status()
            payload = response.json()
            result = payload.get("result")
            if payload.get("success") is not True or not isinstance(result, dict):
                raise ValueError(f"Provider did not return validated history: {payload.get('message')}")
            count, reported_pages = int(result["count"]), int(result["pages"])
            if page == 1:
                expected, pages = count, reported_pages
            elif expected != count or pages != reported_pages:
                raise ValueError("Provider pagination changed; discard the staged attempt")
            if count < 0 or count > len(batch) * len(sessions) or pages != math.ceil(count / 1000):
                raise ValueError("Unexpected provider pagination/count")
            data = result.get("data") or []
            if not isinstance(data, list) or len(data) > 1000:
                raise ValueError("Invalid provider response rows")
            responses.append({"params": {**params, "pageNumber": str(page)}, "payload": payload})
            for raw in data:
                code = raw.get("SECURITY_CODE")
                day = date.fromisoformat(str(raw.get("TRADE_DATE", ""))[:10]).isoformat()
                if code not in batch or day not in sessions or (code, day) in seen:
                    raise ValueError("Out-of-scope, nontrading or duplicated provider row")
                seen.add((code, day))
                values = [float(raw[key]) for key in SOURCE_COLUMNS]
                if any(not math.isfinite(value) for value in values) or any(value < 0 for value in values[2:]):
                    raise ValueError("Incomplete/nonfinite valuation; no fabricated fill is allowed")
                if (symbols[code], day) not in existing:
                    records.append({"symbol_id": symbols[code], "symbol": code, "trade_date": day,
                                    **dict(zip(TARGET_COLUMNS, values)), "raw": raw})
            received += len(data)
            page += 1
            time.sleep(.15)
        if received != expected:
            raise ValueError("Incomplete provider pagination")
        print(json.dumps({"stage_symbols": min(offset + 200, len(codes)), "total_symbols": len(codes), "new_rows": len(records)}), flush=True)
    evidence = {"dataset": "stock_valuations", "database": "gpfx_validation", "start": str(start), "end": str(end),
        "sessions": sorted(sessions), "symbols": symbols, "source_url": SOURCE_URL,
        "records": records, "responses": responses, "observed_at": datetime.now(timezone.utc).isoformat()}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"staged": len(records), "file": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}), flush=True)


def apply(target, expected_hash, baseline):
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("Staged evidence SHA256 mismatch")
    evidence = json.loads(target.read_text(encoding="utf-8"))
    before = json.loads(baseline.read_text(encoding="utf-8"))
    if evidence["database"] != "gpfx_validation" or evidence["dataset"] != "stock_valuations" or before["database"] != "gpfx_validation":
        raise ValueError("Wrong dataset/database")
    records = evidence["records"]
    if not records:
        print("No missing rows to insert")
        return
    # Only INSERT is issued. Unique-key collisions abort the entire transaction.
    with connect() as db:
        try:
            with db.cursor() as cursor:
                cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='stock_valuations'")
                if (cursor.fetchone() or {}).get("ENGINE", "").lower() != "innodb":
                    raise ValueError("Insert/rollback safety requires the existing InnoDB table")
                cursor.execute("SELECT COUNT(*) n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='stock_valuations'")
                if cursor.fetchone()["n"]:
                    raise ValueError("Insert triggers require a separate preservation review")
                cursor.execute("SELECT COUNT(*) n FROM async_tasks WHERE status IN ('running','queued')")
                if cursor.fetchone()["n"]:
                    raise ValueError("Stop other writers before applying a frozen baseline")
                cursor.execute("SELECT COUNT(*) n,COALESCE(MAX(id),0) max_id FROM stock_valuations")
                actual = cursor.fetchone()
                original = before["tables"]["stock_valuations"]
                if (actual["n"], actual["max_id"]) != (original["total"], original["max_id"]):
                    raise ValueError("Valuation baseline changed; recapture before applying")
                values = []
                seen = set()
                for row in records:
                    day = row["trade_date"]
                    key = (row["symbol_id"], day)
                    if key in seen or evidence["symbols"].get(row["symbol"]) != row["symbol_id"] or day not in evidence["sessions"]:
                        raise ValueError("Stage symbol/date identity mismatch")
                    seen.add(key)
                    numbers = [float(row[field]) for field in TARGET_COLUMNS]
                    if not all(math.isfinite(value) for value in numbers):
                        raise ValueError("Stage contains nonfinite key fields")
                    values.append((row["symbol_id"], day, *numbers, "eastmoney:valuation_backfill",
                                   json.dumps(row["raw"], ensure_ascii=False), datetime.now(timezone.utc).replace(tzinfo=None)))
                cursor.executemany("""INSERT INTO stock_valuations
                    (symbol_id,trade_date,pe_ttm,pb,total_market_cap,circulating_market_cap,source,raw_json,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", values)
                inserted = cursor.rowcount
                cursor.execute("SELECT COUNT(*) n FROM stock_valuations")
                if inserted != len(values) or cursor.fetchone()["n"] != original["total"] + len(values):
                    raise ValueError("Insert accounting mismatch; rollback")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    receipt = {"table": "stock_valuations", "inserted": inserted, "before": original["total"],
               "after": original["total"] + inserted, "stage_sha256": expected_hash,
               "start": evidence["start"], "end": evidence["end"]}
    target.with_suffix(".receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stage", "apply"))
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--sha256")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.mode == "stage":
        if args.start is None or args.end is None:
            parser.error("stage requires explicit --start and --end")
        stage(args.start, args.end, args.file)
    else:
        if not args.sha256 or not args.baseline:
            parser.error("apply requires --sha256 and --baseline")
        apply(args.file, args.sha256, args.baseline)


if __name__ == "__main__":
    main()
