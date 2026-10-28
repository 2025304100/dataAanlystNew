"""Restore idle local backend/frontend with model, scheduler and delivery disabled."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time
from urllib.request import build_opener, ProxyHandler

import psutil
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import dev_services
from scripts.restart_backend_validation import snapshot, identity


def invariants():
    state, url = snapshot()
    engine = create_engine(url, hide_parameters=True,
        connect_args={"init_command": "SET SESSION TRANSACTION READ ONLY", "connect_timeout": 8})
    hashes = {}
    tables = ("market_signal_evidence", "market_signal_assessments", "market_signal_predictions",
        "market_signal_outcomes", "market_signal_settings", "notification_outbox", "notification_deliveries",
        "ai_profiles", "portfolios")
    with engine.connect() as connection:
        queries = {table: (f"SELECT * FROM `{table}` ORDER BY id", {}) for table in tables}
        queries.update({
            "original_task": ("SELECT * FROM async_tasks WHERE id=:id", {"id": "aa250d8c3fb7419bad5492bd78b46079"}),
            "original_plan": ("SELECT * FROM data_sync_plans WHERE id=:id", {"id": "25abe7418c33493982b1f8f849ae0c30"}),
            "original_partitions": ("SELECT * FROM data_sync_partitions WHERE plan_id=:id ORDER BY id",
                {"id": "25abe7418c33493982b1f8f849ae0c30"}),
        })
        for name, (query, params) in queries.items():
            digest, count = hashlib.sha256(), 0
            for row in connection.execute(text(query), params).mappings():
                digest.update(json.dumps(dict(row), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), default=str).encode("utf-8")+b"\n")
                count += 1
            hashes[name] = {"rows": count, "sha256": digest.hexdigest()}
        state["capital_flows"] = dict(connection.execute(text(
            "SELECT COUNT(*) AS rows_count,MAX(id) AS max_id,MAX(trade_date) AS max_date FROM capital_flows")).mappings().one())
        state["existing_rules"] = {str(row.id): hashlib.sha256(json.dumps(dict(row._mapping),
            ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            for row in connection.execute(text("SELECT * FROM market_signal_rules ORDER BY id"))}
        state["existing_versions"] = {str(row.id): hashlib.sha256(json.dumps(dict(row._mapping),
            ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            for row in connection.execute(text("SELECT * FROM market_signal_rule_versions ORDER BY id"))}
        connection.rollback()
    engine.dispose()
    return json.loads(json.dumps({"state": state, "content_hashes": hashes}, default=str))


def _listener(port):
    owners = {c.pid for c in psutil.net_connections(kind="tcp")
        if c.status == psutil.CONN_LISTEN and c.laddr.port == port}
    if len(owners) > 1 or None in owners:
        raise RuntimeError("Ambiguous local service owner; no process will be stopped")
    return next(iter(owners), None)


def _verify_process(pid, cwd, command):
    value = identity(pid)
    same = lambda a, b: os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    executable_matches = (same(value["exe"], command[0]) if Path(command[0]).is_absolute()
        else Path(value["exe"]).stem.casefold() == Path(command[0]).stem.casefold())
    if not executable_matches or not same(value["cwd"], cwd) or value["command"][1:] != command[1:]:
        raise RuntimeError("Unexpected local service identity; no process will be stopped")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    folder = args.evidence_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    output = folder/"restore.json"
    if output.exists():
        parser.error("Use a new evidence directory")
    result = {"passed": False, "started_at": datetime.now(timezone.utc).isoformat(), "validation_mode": True}
    try:
        before = invariants()
        (folder/"before-invariants.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        state = before["state"]
        if state["revision"] != "wps_0024_092_market_signals" or state["active_async"] or state["active_discovery"]:
            raise RuntimeError("0092 and completely idle background work are required")
        if state["signal_counts"]["market_signal_settings"] or state["monitor_count"]:
            raise RuntimeError("Expected the previously verified disabled signal settings and monitor")
        if any(row["status"] == "sending" and row["total"] for row in state["outbox"]):
            raise RuntimeError("A notification is being delivered; wait without stopping it")
        _, url = snapshot()
        # Never use old PIDs for control. Occupied ports must match the current
        # project and already run in acceptance mode; otherwise stop here.
        backend_pid, frontend_pid = _listener(8000), _listener(5173)
        opener = build_opener(ProxyHandler({}))
        if backend_pid:
            _verify_process(backend_pid, ROOT, dev_services.backend_command())
            with opener.open("http://127.0.0.1:8000/health", timeout=3) as response:
                if response.headers.get("X-App-Validation-Mode") != "1":
                    raise RuntimeError("Existing backend is not protected; use guarded restart after checking active work")
        if frontend_pid:
            _verify_process(frontend_pid, ROOT/"frontend", dev_services.vite_command())
        latest, _ = snapshot()
        if latest["active_async"] or latest["active_discovery"]:
            raise RuntimeError("New work appeared; restoration deferred without stopping it")
        # A detached console can still belong to an outer executor Job.
        # Task Scheduler owns the persistent protected host independently.
        from scripts.validation_service_host import start as start_managed_host
        managed = start_managed_host()
        result["managed_host"] = managed["host"]
        result["hosting"] = "on-demand-windows-task"
        backend_pid, frontend_pid = _listener(8000), _listener(5173)
        if not backend_pid or not frontend_pid:
            raise RuntimeError("Managed host readiness did not produce both service listeners")
        result["backend"] = _verify_process(backend_pid, ROOT, dev_services.backend_command())
        result["frontend"] = _verify_process(frontend_pid, ROOT/"frontend", dev_services.vite_command())
        if _listener(8000) != backend_pid or _listener(5173) != frontend_pid:
            raise RuntimeError("Local port ownership changed during restoration")
        after = invariants()
        (folder/"after-invariants.json").write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
        if before != after:
            raise RuntimeError("Business state changed during restoration; inspect invariant evidence")
        result.update(passed=True, business_state_unchanged=True, real_model_calls=0, notifications_sent=0,
            migrations_executed=0, processes_stopped=0)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            result["reason"] = str(exc)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
