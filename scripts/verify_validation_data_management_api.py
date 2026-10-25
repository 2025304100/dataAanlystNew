"""Verify the dedicated, local read-only validation runtime and its audit APIs."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]


def verify(base):
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Only the local validation runtime may be checked")
    session = requests.Session(); session.trust_env = False
    status = session.get(base + "/api/v1/validation-data-audit/status", timeout=20)
    status.raise_for_status(); runtime = status.json()
    assert runtime["read_only"] and runtime["database"] == "gpfx_validation"
    assert runtime["purpose"] == "validation_data_audit"
    assert not any(runtime[k] for k in ("background_jobs_enabled", "migrations_enabled", "startup_repairs_enabled",
                                       "task_registration_allowed", "task_dispatch_allowed"))
    assert all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in runtime["loaded_files"].items())
    checks = []
    paths = ["/api/v1/validation-data-audit/status", "/api/v1/inspection-acceptance/status", "/health",
             "/api/v1/system/tasks?limit=100", "/api/v1/external-data/source-field-integrity",
             "/api/v1/external-data/source-field-integrity?as_of=2026-10-22",
             "/api/v1/external-data/coverage", "/api/v1/external-data/overview",
             "/api/v1/external-data/quality-snapshots", "/api/v1/external-data/sync-capabilities",
             "/api/v1/system/data-health", "/api/v1/universe/stats", "/api/v1/index-prices/status",
             "/api/v1/external-data/gaps?dataset=capital_flow&start_date=2026-10-14&end_date=2026-10-16",
             "/api/v1/external-data/source-field-integrity?as_of=2099-01-01",
             "/api/v1/external-data/source-field-integrity?as_of=bad-date"]
    skipped_count = 0
    for path in paths:
        response = session.get(base + path, timeout=(5, 90))
        body = response.json()
        checks.append(dict(method="GET", path=path, status=response.status_code, body=body))
        assert response.status_code == (422 if "2099" in path or "bad-date" in path else 200), (path, response.status_code)
        if path.endswith("limit=100"):
            skipped = [t for t in body["tasks"] if (t.get("result") or {}).get("skip_reason") == "market_closed"
                       and not (t.get("result") or {}).get("records")]
            skipped_count = len(skipped)
            assert skipped_count >= 7 and all(t["result"]["outcome"] == "skipped" for t in skipped)
        if path.endswith("source-field-integrity"):
            assert body["window_basis"] == "asia_shanghai_today" and len(body["fields"]) == 39
            assert all(r["nonnull"] + r["missing"] == r["total"] for r in body["fields"])
        if "as_of=2026-10-22" in path:
            assert body["as_of"] == "2026-10-22" and body["window_basis"] == "explicit_as_of"
    # Only after proving this is the process-local read-only launcher: verify
    # middleware rejects requests before handler validation or task registration.
    for path in ("/api/v1/external-data/quality-snapshots/refresh", "/api/v1/universe/incremental-sync",
                 "/api/v1/external-data/gaps/repair"):
        response = session.post(base + path, json={}, timeout=15)
        body = response.json()
        checks.append(dict(method="POST", path=path, status=response.status_code, body=body))
        assert response.status_code == 423
        assert body["request_registered"] is False and body["task_dispatched"] is False
    return {"passed": True, "runtime": runtime, "holiday_skips": skipped_count, "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists(): parser.error("Do not overwrite existing evidence")
    result = verify(args.base.rstrip("/"))
    with args.report.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]),
                      "holiday_skips": result["holiday_skips"], "pid": result["runtime"]["pid"]}))
