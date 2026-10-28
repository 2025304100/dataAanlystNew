"""Restart one verified idle project backend in temporary API acceptance mode.

No frontend restart, task cancellation, model invocation or notification send.
Keep APP_VALIDATION_MODE=1 until the operator explicitly resumes normal work.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import build_opener, ProxyHandler

import psutil
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import dev_services


def snapshot():
    cfg = json.loads((ROOT/"config/db_config.json").read_text(encoding="utf-8-sig"))
    if not cfg.get("use_mysql") or cfg["mysql"]["database"] != "gpfx":
        raise RuntimeError("Expected the previously verified gpfx database")
    mysql = cfg["mysql"]
    url = URL.create("mysql+pymysql",host=mysql["host"],port=int(mysql.get("port",3306)),username=mysql["user"],
        password=mysql.get("password",""),database="gpfx",query={"charset":"utf8mb4"})
    engine = create_engine(url,hide_parameters=True,connect_args={"init_command":"SET SESSION TRANSACTION READ ONLY","connect_timeout":8})
    with engine.connect() as connection:
        rows = lambda sql: [dict(row) for row in connection.execute(text(sql)).mappings()]
        result = {"revision":connection.scalar(text("SELECT version_num FROM alembic_version")),
            "profiles":rows("SELECT id,daily_request_count,daily_request_reset_at,updated_at FROM ai_profiles ORDER BY id"),
            "deliveries":rows("SELECT status,COUNT(*) AS total,MAX(id) AS max_id FROM notification_deliveries GROUP BY status ORDER BY status"),
            "outbox":rows("SELECT status,COUNT(*) AS total,MAX(id) AS max_id FROM notification_outbox GROUP BY status ORDER BY status"),
            "active_async":rows("SELECT id,task_type,status,processed,total,heartbeat_at,updated_at FROM async_tasks WHERE status IN ('queued','running','pending','cancelling')"),
            "active_discovery":rows("SELECT id,status FROM discovery_tasks WHERE status IN ('queued','running','pending','cancelling')"),
            "signal_counts":{table:connection.scalar(text(f"SELECT COUNT(*) FROM `{table}`")) for table in (
                "market_signal_rules","market_signal_rule_versions","market_signal_evidence","market_signal_assessments",
                "market_signal_predictions","market_signal_settings","market_signal_outcomes")},
            "monitor_count":connection.scalar(text("SELECT COUNT(*) FROM scheduled_tasks WHERE task_type='market_signals_monitor'"))}
        connection.rollback()
    engine.dispose()
    return json.loads(json.dumps(result, default=str)), url


def identity(pid):
    process = psutil.Process(pid)
    return {"pid":pid,"created":process.create_time(),"cwd":process.cwd(),"exe":process.exe(),"command":process.cmdline()}


def validate_owner(value, expected_created):
    expected = [str(Path("C:/Python312/python.exe")),"-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000"]
    canonical = lambda value: os.path.normcase(os.path.abspath(value))
    if (canonical(value["cwd"]) != canonical(ROOT) or canonical(value["exe"]) != canonical(expected[0])
            or value["command"][1:] != expected[1:] or abs(value["created"]-expected_created)>0.01):
        raise RuntimeError("Backend process identity changed or is not this project")
    listeners = {item.pid for item in psutil.net_connections(kind="tcp") if item.status==psutil.CONN_LISTEN and item.laddr.port==8000}
    if listeners != {value["pid"]}:
        raise RuntimeError("Unexpected process owns port 8000")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-pid",type=int,required=True)
    parser.add_argument("--expected-created",type=float,required=True)
    parser.add_argument("--evidence-dir",type=Path,required=True)
    args = parser.parse_args()
    folder = args.evidence_dir.resolve()
    folder.mkdir(parents=True,exist_ok=True)
    output = folder/"restart.json"
    if output.exists():
        parser.error("Use a new evidence directory")
    report = {"passed":False,"started_at":datetime.now(timezone.utc).isoformat(),"validation_mode":True}
    try:
        before_process = identity(args.expected_pid)
        validate_owner(before_process,args.expected_created)
        report["old_process"] = before_process
        report["before"],url = snapshot()
        if report["before"]["revision"] != "wps_0024_092_market_signals":
            raise RuntimeError("0092 is required before loading the new APIs")
        if report["before"]["active_async"] or report["before"]["active_discovery"]:
            raise RuntimeError("Active work exists; refusing to terminate it")
        if any(row["status"]=="sending" and row["total"] for row in report["before"]["outbox"]):
            raise RuntimeError("A notification transport is active; wait for completion")
        (folder/"before.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
        # Verify instance identity again immediately before the single-PID stop.
        validate_owner(identity(args.expected_pid),args.expected_created)
        latest,_ = snapshot()
        if latest["active_async"] or latest["active_discovery"]:
            raise RuntimeError("Work started during preflight; backend remains running")
        if any(row["status"]=="sending" and row["total"] for row in latest["outbox"]):
            raise RuntimeError("A notification transport started during preflight; backend remains running")
        report["before"] = latest
        old = psutil.Process(args.expected_pid)
        old.terminate()
        old.wait(timeout=15)
        if not dev_services.wait_for_port_closed(8000,5):
            raise RuntimeError("Port 8000 did not close; no other process will be stopped")
        env = dict(os.environ,APP_VALIDATION_MODE="1",AUTO_TRADE_CALENDAR_REFRESH="0",AUTO_UNIVERSE_SYNC_ON_STARTUP="0",
            DATABASE_URL=url.render_as_string(hide_password=False),PYTHONIOENCODING="utf-8")
        out_log,err_log = dev_services.service_logs("backend")
        process = dev_services.launch_process(dev_services.backend_command(),ROOT,out_log,err_log,env=env)
        report["new_pid"] = process.pid
        report["stop_method"] = "Terminate only the verified idle backend PID; no descendants or frontend were stopped"
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic()+55
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise RuntimeError("New backend exited; inspect backend.err.log")
            try:
                with opener.open("http://127.0.0.1:8000/health",timeout=2) as response:
                    if response.status==200 and response.headers.get("X-App-Validation-Mode")=="1":
                        break
            except OSError:
                pass
            time.sleep(.5)
        else:
            raise RuntimeError("Validation backend readiness timed out")
        service_state = dev_services.load_state()
        record = dev_services.record_for("backend",process.pid,8000,"uvicorn",ROOT,dev_services.backend_command(),out_log,err_log)
        record["validation_mode"] = True
        record["background_work_enabled"] = False
        service_state["backend"] = record
        dev_services.save_state(service_state)
        report["new_process"] = identity(process.pid)
        report["after"],_ = snapshot()
        for key in ("profiles","deliveries","outbox","signal_counts","monitor_count"):
            if report["before"][key] != report["after"][key]:
                raise RuntimeError(f"Unexpected acceptance-start change: {key}")
        report.update(passed=True,frontend_untouched=True,model_and_delivery_counters_equal=True)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc,RuntimeError):
            report["reason"] = str(exc)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("passed","old_process","new_pid","error_type","reason") if key in report},ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
