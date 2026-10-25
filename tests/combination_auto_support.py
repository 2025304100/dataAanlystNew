"""Real MySQL/HTTP acceptance fixture with no model, Score or gate stubs."""
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import time

import httpx
from sqlalchemy import create_engine, insert, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]

def migrate(raw):
    url = make_url(raw)
    assert url.get_backend_name() == "mysql" and (url.database or "").startswith("codex_combination_")
    engine = create_engine(url, hide_parameters=True)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT VERSION()")) == "5.7.26"
        assert not inspect(conn).get_table_names(), "Use a new empty schema; old evidence is retained"
    env = dict(os.environ, APP_DATABASE_URL=raw, ALEMBIC_DATABASE_URL=raw, PYTHONIOENCODING="utf-8")
    run = subprocess.run([sys.executable, "scripts/guarded_database_migration.py", "upgrade",
        "--expected-database", url.database, "--expected-revision", "base", "--expected-server-version", "5.7.26",
        "--target-revision", "wps_0024_087_binding_precision"], env=env, cwd=ROOT,
        capture_output=True, encoding="utf-8", timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    return engine, json.loads(run.stdout)

def seed_market(engine, evidence):
    from app.models.factor_runtime import FactorSystemConfig
    from app.models.symbol import Symbol
    from app.models.universe import UniverseSymbol, UniverseDailyBar
    from app.models.daily_bar import DailyBar
    from app.models.stock_valuation import StockValuation
    from app.models.market_data import MarketCalendar
    from app.models.app_setting import AppSetting
    from app.services.factors.definitions import seed_factor_definitions
    from app.services.portfolio_reconciliation_events import install_source_receipts
    rng = random.Random(20261025)
    days = []
    day = date(2026, 10, 20)
    while len(days) < 280:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    with Session(engine) as db:
        db.add(FactorSystemConfig(id=1, feature_enabled=1,
            warehouse_path=str((evidence/"warehouse.duckdb").resolve()), extra_json="{}", updated_by="isolated-e2e"))
        seed_factor_definitions(db)
        for year in (2025, 2026, 2027):
            first = date(year, 1, 1)
            annual = [first+timedelta(days=n) for n in range((date(year+1,1,1)-first).days)]
            closed = set([d for d in annual if d.weekday()<5][:21])
            opened = {d for d in annual if d.weekday()<5 and d not in closed}
            db.add_all(MarketCalendar(trade_date=d, is_trading_day=int(d in opened)) for d in annual)
            if year == 2026:
                db.add(AppSetting(key="market.cn_trade_calendar", updated_by="exchange-calendar-sync", value_json=json.dumps({
                    "source":"https://www.sse.com.cn/disclosure/dealinstruc/closed/", "year":year,
                    "refreshed_at":"2026-10-19T00:00:00+00:00", "trading_days":len(opened),
                    "source_version":"synthetic-combination-auto-e2e-2026"})))
        ids=[]
        for number in range(100):
            symbol=Symbol(symbol=f"{600001+number}", name=f"E2E synthetic {number}", market="SH", asset_type="stock", is_active=1)
            universe=UniverseSymbol(symbol=symbol.symbol,name=symbol.name,market="sh",asset_type="stock",region="cn",
                is_synced=1,last_bar_date=days[-1],bar_count=len(days))
            db.add_all([symbol,universe]);db.flush();ids.append(symbol.id)
            beta=(number-49.5)/50
            previous=10.0
            bars=[]; valuations=[]; business=[]
            for index, day in enumerate(days):
                close=previous*math.exp(.0005*beta+rng.gauss(0,.01))
                observed=datetime.combine(day,datetime.min.time())+timedelta(hours=7)
                value=dict(trade_date=day,open=previous,high=max(close,previous)*1.01,low=min(close,previous)*.99,
                    close=close,volume=1e6,amount=close*1e6,turnover_rate=2+beta,source="isolated.synthetic",created_at=observed)
                bars.append(dict(universe_symbol_id=universe.id,**value))
                business.append(dict(symbol_id=symbol.id,**value))
                valuations.append(dict(symbol_id=symbol.id,trade_date=day,pe_ttm=1/(.05+.015*beta),pb=2,
                    source="isolated.synthetic",created_at=observed))
                previous=close
            db.execute(insert(UniverseDailyBar),bars);db.execute(insert(DailyBar),business)
            db.execute(insert(StockValuation),valuations)
        db.commit()
    install_source_receipts()
    return {"days":[d.isoformat() for d in days],"symbol_ids":ids,"synthetic":True,"training_gate_overrides":{}}

def source_hashes(engine, bounds=None):
    hashes={}
    with engine.connect() as conn:
        for name in ("symbols","universe_symbols","universe_daily_bars","daily_bars","stock_valuations"):
            digest=hashlib.sha256(); count=0
            maximum=bounds[name]['max_id'] if bounds else conn.scalar(text(f'SELECT COALESCE(MAX(id),0) FROM {name}'))
            for row in conn.execution_options(stream_results=True).execute(text(f"SELECT * FROM {name} WHERE id<=:maximum ORDER BY id"),{'maximum':maximum}).mappings():
                digest.update(json.dumps(dict(row),default=str,sort_keys=True,separators=(",",":")).encode())
                count+=1
            hashes[name]={"rows":count,"max_id":maximum,"sha256":digest.hexdigest()}
    return hashes

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));return sock.getsockname()[1]

def start_api(raw,evidence,port=None):
    port=port or free_port()
    vendor=ROOT/"tmp/combination-auto-e2e-20261025/vendor"
    env=dict(os.environ,DATABASE_URL=raw,AUTO_UNIVERSE_SYNC_ON_STARTUP="0",AUTO_TRADE_CALENDAR_REFRESH="0",
        COMBINATION_E2E_CLOCK_UTC="2026-10-20T08:00:00",FACTOR_PIPELINE_RSS_LIMIT_MB="2048",
        PYTHONPATH=os.pathsep.join([str(ROOT/"tests/e2e_runtime"),str(vendor),str(ROOT)]),PYTHONIOENCODING="utf-8")
    log=(evidence/"api.log").open("w",encoding="utf-8")
    process=subprocess.Popen([sys.executable,"-m","uvicorn","tests.combination_auto_runtime:app",
        "--host","127.0.0.1","--port",str(port)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    url=f"http://127.0.0.1:{port}"
    for _ in range(240):
        if process.poll() is not None:raise RuntimeError((evidence/"api.log").read_text(encoding="utf-8")[-7000:])
        try:
            if httpx.get(url+"/health",timeout=1,trust_env=False).status_code==200:return process,url,log
        except httpx.HTTPError:pass
        time.sleep(.25)
    raise RuntimeError("Isolated API did not become healthy")

def wait_task(client, task_id, timeout=240):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        response=client.get(f"/api/v1/scoring/tasks/{task_id}");response.raise_for_status();task=response.json()
        if task["status"] in {"done","failed","cancelled"}:return task
        time.sleep(.25)
    raise AssertionError(f"Real pipeline task {task_id} timed out")
