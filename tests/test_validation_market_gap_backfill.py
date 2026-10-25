"""Validation rollout: bounded source identity, insert atomicity and safe startup."""
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.services import capital_flow_history_provider as provider
from scripts import backfill_validation_market_gaps as runbook

import pytest

pytestmark = pytest.mark.whitebox



def payload():
    return {"rc": 0, "data": {"code": "600519", "market": 1,
        "klines": ["2026-10-14,100,-20,-80,40,60,1,-.2,-.8,.4,.6,1200,1,0,0"]}}


def test_expanded_szse_etf_family_is_recognized_without_relaxing_non_etfs():
    from app.services.market_history_contract import cn_history_instrument
    from app.services.market_data_sources.sources.tencent_etf_history import _instrument
    from types import SimpleNamespace
    assert cn_history_instrument('158000') == {'asset_type': 'etf', 'market': 'sz'}
    assert _instrument(SimpleNamespace(symbol='158000', market='sz', asset_type='etf')) == 'sz158000'
    with pytest.raises(ValueError):
        _instrument(SimpleNamespace(symbol='158000', market='sh', asset_type='etf'))
    with pytest.raises(ValueError):
        cn_history_instrument('511990')


@pytest.mark.parametrize("damage", ["symbol", "market", "columns", "null", "duplicate", "overflow"])
def test_corrupt_or_misidentified_flow_history_rejected_as_a_whole(damage):
    body = payload()
    if damage == "symbol": body["data"]["code"] = "600428"
    if damage == "market": body["data"]["market"] = 0
    if damage == "columns": body["data"]["klines"][0] += ",extra"
    if damage == "null": body["data"]["klines"][0] = body["data"]["klines"][0].replace(",100,", ",nan,")
    if damage == "duplicate": body["data"]["klines"] *= 2
    if damage == "overflow": body["data"]["klines"] *= 121
    with pytest.raises((ValueError, TypeError)):
        provider.parse_capital_flow_payload(body, stock="600519", market="sh")


def test_flow_network_is_bounded_and_preserves_original_payload(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        content = json.dumps(payload()).encode()
        def raise_for_status(self): pass
        def json(self): return payload()
    class HTTP:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr(provider.requests, "Session", HTTP)
    frame = provider.fetch_capital_flow_history(stock="600519", market="sh")
    assert calls[0][1]["params"]["lmt"] == "120"
    assert calls[0][1]["timeout"] == (5, 15)
    assert frame.iloc[0]["主力净流入-净额"] == 100
    assert frame.iloc[0]["超大单净流入-净额"] == 60
    assert frame.attrs["source_receipt"]["payload"] == payload()


def test_gap_apply_rolls_back_conflict_and_preserves_every_old_column(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    raw = sqlite3.connect(path)
    fields = ",".join(f"{field} REAL" for field in runbook.CF_FIELDS)
    raw.executescript(f"""CREATE TABLE async_tasks(status TEXT);
        CREATE TABLE symbols(id INTEGER,symbol TEXT,asset_type TEXT,market TEXT);
        INSERT INTO symbols VALUES(1,'600519','stock','sh');
        CREATE TABLE capital_flows(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol_id INTEGER,trade_date TEXT,
        {fields},source TEXT,created_at TEXT,raw_json TEXT,UNIQUE(symbol_id,trade_date));
        INSERT INTO capital_flows VALUES(1,1,'2026-10-15',1,2,3,4,5,6,'old','oldstamp','oldraw');""")
    raw.commit()
    original = raw.execute("SELECT * FROM capital_flows").fetchall()
    class Cursor:
        def __init__(self, db): self.c = db.cursor(); self.fake = None
        def __enter__(self): return self
        def __exit__(self, *args): self.c.close()
        def execute(self, sql, args=()):
            self.fake = None
            if sql.startswith("SET SESSION"): return
            if "information_schema.TABLES" in sql: self.fake = {"ENGINE": "InnoDB"}; return
            if "information_schema.TRIGGERS" in sql: self.fake = {"n": 0}; return
            self.c.execute(sql.replace("%s", "?"), args)
        def executemany(self, sql, args): self.c.executemany(sql.replace("%s", "?"), args)
        def fetchone(self): return self.fake or dict(self.c.fetchone())
        def fetchall(self): return [dict(row) for row in self.c.fetchall()]
        @property
        def rowcount(self): return self.c.rowcount
    class Connection:
        def __init__(self): self.db=sqlite3.connect(path); self.db.row_factory=sqlite3.Row
        def cursor(self): return Cursor(self.db)
        def commit(self): self.db.commit()
        def rollback(self): self.db.rollback()
        def __enter__(self): return self
        def __exit__(self, *args): self.db.close()
    monkeypatch.setattr(runbook, "connect", Connection)
    scope={"database":"gpfx_validation","capital":[{"id":1,"symbol":"600519","market":"sh","missing":["2026-10-14","2026-10-15"]}]}
    runbook.save(tmp_path / "scope.json",scope)
    runbook.save(tmp_path / "before.json",{"database":"gpfx_validation","tables":{"capital_flows":{"total":1,"max_id":1}}})
    row={"symbol_id":1,"symbol":"600519","trade_date":"2026-10-14",**dict.fromkeys(runbook.CF_FIELDS,10.),"raw":{"observed":True}}
    manifest={"database":"gpfx_validation","dataset":"capital","scope_sha256":runbook.digest(tmp_path/'scope.json'),"files":{},"records":[row,{**row,"trade_date":"2026-10-15"}]}
    stage=tmp_path/'manifest.json';runbook.save(stage,manifest)
    with pytest.raises(sqlite3.IntegrityError):
        runbook.apply(stage,runbook.digest(stage),tmp_path/'before.json')
    assert raw.execute("SELECT * FROM capital_flows").fetchall()==original
    stage2=tmp_path/'verified.json';manifest['records']=[row];runbook.save(stage2,manifest)
    runbook.apply(stage2,runbook.digest(stage2),tmp_path/'before.json')
    assert raw.execute("SELECT * FROM capital_flows WHERE id=1").fetchall()==original
    assert raw.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0]==2
    with pytest.raises(ValueError,match="Baseline changed"):
        runbook.apply(stage2,runbook.digest(stage2),tmp_path/'before.json')


def test_safe_launcher_uses_current_app_without_startup_jobs_or_initialization():
    # The process-level read-only guard must not leak into unrelated pytest tests.
    program = '''
import os
os.environ['DATABASE_URL']='mysql+pymysql://unused:unused@127.0.0.1/gpfx_validation'
from scripts.serve_validation_data_audit import build_app
app=build_app('reviewed-revision')
import app.main as main
def forbidden(*a,**kw): raise AssertionError('ordinary startup side effect invoked')
for name in ('init_db','_run_startup_cleanup','_reap_stale_mining_runs','_auto_start_universe_init','scheduler_loop','_periodic_cleanup'):
 setattr(main,name,forbidden)
class Conn:
 def __enter__(self): return self
 def __exit__(self,*a): pass
 def scalar(self,stmt): return 'gpfx_validation' if str(stmt)=='SELECT DATABASE()' else 0
 def scalars(self,stmt): return ['reviewed-revision']
class Manager:
 def initialize(self,*a,**kw): pass
 def connect(self): return Conn()
 def dispose(self): pass
mgr=Manager();mgr.engine=mgr
from app.db.manager import DatabaseManager
DatabaseManager.get=classmethod(lambda cls:mgr)
from fastapi.testclient import TestClient
with TestClient(app) as client:
 r=client.get('/api/v1/validation-data-audit/status')
 assert r.status_code==200 and r.json()['revision']=='reviewed-revision'
 assert r.json()['background_jobs_enabled'] is False
 assert client.get('/api/v1/inspection-acceptance/status').json()['mode']=='inspection_acceptance'
 assert client.post('/api/v1/universe/incremental-sync',json={}).status_code==423
print('safe-lifespan-and-http-boundary-passed')
'''
    result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'safe-lifespan-and-http-boundary-passed' in result.stdout
