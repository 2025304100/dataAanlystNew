"""Fresh loopback MySQL 5.7 schemas; real APIs, static frontend, fake AI vendor only.

No configured business database is selected, reused, dropped, or migrated.
Created schemas are retained. No production service is restarted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_market_news_acceptance import run_suite, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-connection-config", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--stage", choices=("mysql", "e2e", "all"), default="all")
    args = parser.parse_args()
    revision_file = ROOT/"alembic/versions/2026_10_27_0092_market_signals.py"
    revision_hash = hashlib.sha256(revision_file.read_bytes()).hexdigest()
    if not re.fullmatch(r"codex_market_[a-z0-9_]{1,30}", args.prefix):
        parser.error("A new codex_market_ prefix is required")
    mysql = json.loads(args.local_connection_config.read_text(encoding="utf-8-sig"))["mysql"]
    if mysql["host"] not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Only an explicitly authorized loopback MySQL server is accepted")
    url = URL.create("mysql+pymysql", username=mysql["user"], password=mysql.get("password", ""), host=mysql["host"],
        port=int(mysql.get("port", 3306)), query={"charset": "utf8mb4"})
    folder = args.evidence_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if (folder/"result.json").exists():
        parser.error("Use a new evidence directory")
    stages = ("mysql", "e2e") if args.stage == "all" else (args.stage,)
    schemas = {stage: args.prefix+"_"+stage for stage in stages}
    if "mysql" in stages:
        schemas["legacy"] = args.prefix+"_legacy"
    server = create_engine(url, hide_parameters=True)
    with server.begin() as connection:
        identity = dict(connection.execute(text("SELECT VERSION() AS version,DATABASE() AS selected_database,@@server_uuid AS uuid")).mappings().one())
        assert identity["selected_database"] is None and identity["version"].startswith("5.7.")
        existing = set(connection.execute(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")).scalars())
        assert not existing.intersection(schemas.values()), "Never reuse an existing schema"
        for name in schemas.values():
            connection.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    server.dispose()
    identity["server_uuid_sha256"] = hashlib.sha256(identity.pop("uuid").encode()).hexdigest()
    write_json(folder/"identity.json", {**identity, "isolated_schemas": schemas})
    env = dict(os.environ, DATABASE_URL="sqlite:///:memory:", AUTO_TRADE_CALENDAR_REFRESH="0", AUTO_UNIVERSE_SYNC_ON_STARTUP="0",
        MARKET_NEWS_EVIDENCE_DIR=str(folder), PYTHONIOENCODING="utf-8", REQUIRE_LIVE_BACKEND="1",
        FACTOR_WAREHOUSE_PATH=str(folder/"isolated-factors.duckdb"))
    results = []
    if "mysql" in stages:
        env["MARKET_SIGNALS_MYSQL57_URL"] = url.set(database=schemas["mysql"]).render_as_string(hide_password=False)
        env["MARKET_SIGNALS_LEGACY_MYSQL57_URL"] = url.set(database=schemas["legacy"]).render_as_string(hide_password=False)
        results.append(run_suite("signals-mysql57", ["tests/test_market_signals_mysql57.py",
            "tests/test_market_signals_mysql57_existing_tables.py"], env, folder))
    if "e2e" in stages:
        results.append(run_browser(url.set(database=schemas["e2e"]), env, folder))
    source_unchanged = hashlib.sha256(revision_file.read_bytes()).hexdigest() == revision_hash
    final = {"passed": source_unchanged and all(item["passed"] for item in results), "results": results, "schemas_retained": schemas,
             "migration_sha256": revision_hash, "migration_source_unchanged_during_tests": source_unchanged,
             "configured_business_database_selected": False, "third_party_recipients": False}
    write_json(folder/"result.json", final)
    return 0 if final["passed"] else 1


def run_browser(url, env, folder):
    from tests.market_news_acceptance_support import migrate
    migrate(url, "wps_0024_092_market_signals")
    class Vendor(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            analysis = {"event_identity": "验收银行-订单-20261027", "new_facts": ["新增订单100亿元"], "event_type": "demand",
                "relation": "direct", "mechanism": "新增订单直接增加目标银行的业务需求", "material_points": 30,
                "novelty_points": 30, "breadth_points": 5, "duration_points": 10,
                "evidence_quotes": ["验收银行获得新增订单100亿元"], "missing": []}
            payload = json.dumps({"choices": [{"message": {"content": json.dumps(analysis, ensure_ascii=False)}}],
                "usage": {"total_tokens": 150, "prompt_tokens": 100, "completion_tokens": 50}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    vendor = ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
    thread = Thread(target=vendor.serve_forever, daemon=True)
    thread.start()
    engine = create_engine(url, hide_parameters=True)
    from app.models.symbol import Symbol
    from app.models.daily_bar import DailyBar
    from app.models.index_price import IndexPrice
    from app.models.market_data import MarketCalendar
    from app.models.ai_profile import AIProfile
    from app.models.portfolio import Portfolio
    from app.models.market_signal import MarketSignalSettings
    with Session(engine) as db:
        stock = Symbol(symbol="000001", name="验收银行", market="SZ", asset_type="stock")
        db.add(stock)
        db.flush()
        for offset in range(30):
            day = date(2026,10,20)+timedelta(days=offset)
            db.add(MarketCalendar(trade_date=day, is_trading_day=int(day.weekday()<5)))
            if day.weekday()<5:
                db.add(DailyBar(symbol_id=stock.id, trade_date=day, open=100, high=101, low=99, close=100, source="fixture:qfq"))
                db.add(IndexPrice(symbol="000300", trade_date=day, open=100, high=101, low=99, close=100, source="fixture"))
        db.add(AIProfile(name="隔离验收模型", provider="openai_compatible", auth_type="none", model="fixture-model",
            base_url=f"http://127.0.0.1:{vendor.server_port}/v1", purpose="explanation"))
        db.add(MarketSignalSettings(id=1, config_json='{"collect_on_monitor":false}'))
        db.add(Portfolio(name="隔离验收组合", account_type="simulated", total_capital=100000, investable_ratio=.8,
            cash_reserve_ratio=.2, is_default=1, auto_trade_enabled=0))
        db.commit()
    engine.dispose()
    clock_file = folder/"clock.txt"
    clock_file.write_text("2026-10-27T09:10:00", encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env.update(MARKET_NEWS_E2E_MYSQL57_URL=url.render_as_string(hide_password=False), MARKET_SIGNALS_CLOCK_FILE=str(clock_file),
        E2E_BACKEND_URL=origin, E2E_FRONTEND_URL=origin, NO_PROXY="127.0.0.1,localhost")
    write_json(folder/"runtime.json", {"origin": origin, "database": url.database, "lifespan": "off",
        "frontend": "production build", "business_api_mocks": False, "ai_vendor": "local deterministic fixture"})
    log = (folder/"server.log").open("w", encoding="utf-8")
    runtime = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.market_signals_e2e_server:app", "--host", "127.0.0.1",
        "--port", str(port), "--lifespan", "off"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        session = requests.Session()
        session.trust_env = False
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            if runtime.poll() is not None:
                raise RuntimeError("Isolated API exited; inspect server.log")
            try:
                if session.get(origin+"/health", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(.5)
        else:
            raise RuntimeError("Isolated API readiness timed out")
        return run_suite("signals-browser", ["tests/e2e/test_market_signals_acceptance.py"], env, folder)
    finally:
        if runtime.poll() is None:
            runtime.terminate()
            try:
                runtime.wait(timeout=15)
            except subprocess.TimeoutExpired:
                runtime.kill()
                runtime.wait(timeout=5)
        log.close()
        vendor.shutdown()
        vendor.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
