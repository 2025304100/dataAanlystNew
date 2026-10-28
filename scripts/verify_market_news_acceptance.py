"""Run real MySQL 5.7 migration + production-page E2E on fresh isolated schemas.

Explicit local config is used only as server credentials. Its database is never
selected. New codex_market_* schemas are retained; no existing schema is cleared.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def run_suite(name, files, env, folder, timeout=420):
    log, junit = folder / f"{name}.log", folder / f"{name}.xml"
    with log.open("w", encoding="utf-8") as stream:
        run = subprocess.run([sys.executable, "-m", "pytest", *files, "-q", "--tb=short", "--junitxml", str(junit)],
                             cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    if junit.exists():
        for suite in ET.parse(junit).iter("testsuite"):
            for key in counts:
                counts[key] += int(suite.attrib.get(key, 0))
    result = {"name": name, "exit_code": run.returncode, **counts,
              "passed": run.returncode == 0 and counts["tests"] > 0 and not any(counts[k] for k in ("failures", "errors", "skipped"))}
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-connection-config", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--stage", choices=("migration", "e2e", "delivery", "all"), default="all")
    args = parser.parse_args()
    if not re.fullmatch(r"codex_market_[a-z0-9_]{1,32}", args.prefix):
        parser.error("Use a fresh codex_market_ prefix plus 1..32 lowercase letters/digits/underscores")
    config = json.loads(args.local_connection_config.read_text(encoding="utf-8-sig"))["mysql"]
    if config["host"] not in ("127.0.0.1", "localhost", "::1"):
        parser.error("This runner only accepts an explicit loopback test-server connection")
    server_url = URL.create("mysql+pymysql", username=config["user"], password=config.get("password", ""),
                            host=config["host"], port=int(config.get("port", 3306)), query={"charset": "utf8mb4"})
    folder = args.evidence_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    result_path = folder / f"result-{args.stage}.json"
    if result_path.exists():
        parser.error("Evidence result already exists; use a new evidence directory")
    stages = ("migration", "e2e", "delivery") if args.stage == "all" else (args.stage,)
    databases = {stage: f"{args.prefix}_{stage}" for stage in stages}
    if "migration" in stages:
        databases.update(compensation=f"{args.prefix}_compensation", fresh=f"{args.prefix}_fresh")
    server = create_engine(server_url, hide_parameters=True, connect_args={"connect_timeout": 10})
    try:
        with server.connect() as connection:
            identity = dict(connection.execute(text("SELECT VERSION() AS version, @@server_uuid AS server_uuid, "
                "@@port AS port, @@default_storage_engine AS server_default_engine, DATABASE() AS selected_database")).mappings().one())
            if not identity["version"].startswith("5.7.") or identity["selected_database"] is not None:
                raise RuntimeError("Expected MySQL 5.7 server connection without a selected application database")
            existing = set(connection.execute(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")).scalars())
            if existing.intersection(databases.values()):
                raise RuntimeError("Target schema already exists. Existing schemas will never be reused or cleared.")
            for database in databases.values():
                connection.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        server.dispose()
    write_json(folder / f"identity-{args.stage}.json", {**identity, "isolated_schemas": databases,
        "configured_application_database_selected": False, "background_workers": False})
    env = dict(os.environ, DATABASE_URL="sqlite:///:memory:", AUTO_UNIVERSE_SYNC_ON_STARTUP="0",
               AUTO_TRADE_CALENDAR_REFRESH="0", MARKET_NEWS_EVIDENCE_DIR=str(folder),
               FACTOR_WAREHOUSE_PATH=str(folder / "isolated-factor-warehouse.duckdb"), REQUIRE_LIVE_BACKEND="1",
               REQUIRE_MARKET_NEWS_ACCEPTANCE="1", PYTHONIOENCODING="utf-8")
    results = []
    if "migration" in stages:
        env["MARKET_NEWS_MYSQL57_URL"] = server_url.set(database=databases["migration"]).render_as_string(hide_password=False)
        env["MARKET_NEWS_COMPENSATION_MYSQL57_URL"] = server_url.set(database=databases["compensation"]).render_as_string(hide_password=False)
        env["MARKET_NEWS_FRESH_MYSQL57_URL"] = server_url.set(database=databases["fresh"]).render_as_string(hide_password=False)
        results.append(run_suite("mysql57-migration", ["tests/test_market_events_mysql57_acceptance.py",
            "tests/test_market_events_mysql57_compensation.py"], env, folder))
    if "delivery" in stages:
        env["NOTIFICATION_DELIVERY_MYSQL57_URL"] = server_url.set(database=databases["delivery"]).render_as_string(hide_password=False)
        results.append(run_suite("mysql57-delivery", ["tests/test_notification_delivery_mysql57.py"], env, folder))
    if "e2e" in stages:
        from tests.market_news_acceptance_support import migrate, REV92
        url = server_url.set(database=databases["e2e"])
        migrate(url, REV92)
        from sqlalchemy.orm import Session
        from app.models.portfolio import Portfolio
        with Session(create_engine(url)) as db:
            db.add(Portfolio(name="行情消息隔离验收组合", account_type="simulated", total_capital=100000,
                             investable_ratio=0.8, cash_reserve_ratio=0.2, is_default=1, auto_trade_enabled=0))
            db.commit()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        env.update(MARKET_NEWS_E2E_MYSQL57_URL=url.render_as_string(hide_password=False), E2E_BACKEND_URL=origin,
                   E2E_FRONTEND_URL=origin)
        write_json(folder / "runtime.json", {"origin": origin, "database": url.database, "lifespan": "off",
            "frontend": "app/web/dist production build", "business_api_mocks": False})
        runtime_log = (folder / "server.log").open("w", encoding="utf-8")
        runtime = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.market_news_e2e_server:app", "--host", "127.0.0.1",
            "--port", str(port), "--lifespan", "off"], cwd=ROOT, env=env, stdout=runtime_log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            session = requests.Session()
            session.trust_env = False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if runtime.poll() is not None:
                    raise RuntimeError("Isolated API exited; inspect server.log")
                try:
                    if session.get(origin + "/health", timeout=2).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("Isolated API readiness timed out")
            results.append(run_suite("browser-e2e", ["tests/e2e/test_market_news_acceptance.py"], env, folder))
        finally:
            if runtime.poll() is None:
                runtime.terminate()
                try:
                    runtime.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    runtime.kill()
                    runtime.wait(timeout=5)
            runtime_log.close()
    final = {"passed": bool(results) and all(r["passed"] for r in results), "outcomes": results, "schemas_retained": databases}
    write_json(result_path, final)
    return 0 if final["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
