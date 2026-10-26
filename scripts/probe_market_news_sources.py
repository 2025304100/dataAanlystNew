"""Read-only real six-source probe, no application DB and no notifications.

Each supplier runs in a bounded subprocess using the production fetch wrapper.
Only counts/time ranges/hashes are saved; news content and credentials are not.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {"eastmoney-global": "_fetch_global_news", "caixin": "_fetch_caixin_news",
           "cctv": "_fetch_cctv_news", "baidu": "_fetch_baidu_economic_news",
           "baidu-report": "_fetch_baidu_report_news", "futures-shmet": "_fetch_futures_news"}


def source_probe(source):
    os.environ.update(DATABASE_URL="sqlite:///:memory:", AUTO_UNIVERSE_SYNC_ON_STARTUP="0",
                      AUTO_TRADE_CALENDAR_REFRESH="0")
    sys.path.insert(0, str(ROOT))
    from app.services import market_events
    start = time.monotonic()
    try:
        rows = getattr(market_events, SOURCES[source])()
        timestamps = sorted(str(row["published_at"]) for row in rows if row.get("published_at"))
        return dict(source=source, status="ok_nonempty" if rows else "empty", count=len(rows),
            earliest=timestamps[0] if timestamps else None, latest=timestamps[-1] if timestamps else None,
            missing_publication=sum(row.get("published_at") is None for row in rows),
            sample_title_sha256=[hashlib.sha256(row["title"].encode()).hexdigest() for row in rows[:3]],
            elapsed_seconds=round(time.monotonic()-start, 2))
    except Exception as exc:
        # Supplier/proxy exceptions can contain URLs. Do not expose them.
        return dict(source=source, status="error", error_type=type(exc).__name__,
                    category="supplier_cookie_failure" if "cookie" in str(exc).lower() else "supplier_error",
                    cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
                    elapsed_seconds=round(time.monotonic()-start, 2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=tuple(SOURCES))
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    if args.source:
        print(json.dumps(source_probe(args.source), ensure_ascii=False))
        return 0
    if not args.evidence_dir:
        parser.error("--evidence-dir is required")
    folder = args.evidence_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "live-six-sources.json"
    if path.exists():
        parser.error("Use a new evidence directory")
    env = dict(os.environ, DATABASE_URL="sqlite:///:memory:", PYTHONIOENCODING="utf-8",
               AUTO_UNIVERSE_SYNC_ON_STARTUP="0", AUTO_TRADE_CALENDAR_REFRESH="0",
               FACTOR_WAREHOUSE_PATH=str(folder / "isolated-factor-warehouse.duckdb"))

    def run(source):
        try:
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--source", source],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=75,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if child.returncode != 0:
                return dict(source=source, status="process_error", exit_code=child.returncode)
            return json.loads(child.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return dict(source=source, status="timeout", limit_seconds=75)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, SOURCES))
    from datetime import datetime, timezone
    result = dict(executed_at_utc=datetime.now(timezone.utc).isoformat(),
        akshare_version=importlib.metadata.version("akshare"), production_fetch_wrappers=True,
        business_database_selected=False, application_database_writes=False, notifications_sent=False,
        sources=results, all_nonempty=all(row["status"] == "ok_nonempty" for row in results))
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["all_nonempty"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
