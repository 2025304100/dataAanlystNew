"""Production UI and API acceptance mode in a disposable SQLite database."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]


def test_validation_mode_deployed_ui_saves_independent_paused_rules(tmp_path):
    assert (ROOT/"app/web/dist/index.html").exists(), "Build the production frontend before UI acceptance"
    database_url = "sqlite:///"+(tmp_path/"acceptance.db").as_posix()
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Symbol(symbol="000001",name="验收银行",market="SZ",asset_type="stock"))
        db.add(Portfolio(name="隔离验收组合",account_type="simulated",total_capital=100000,
                         investable_ratio=.8,cash_reserve_ratio=.2,is_default=1,auto_trade_enabled=0))
        db.commit()
    engine.dispose()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1",0))
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = dict(os.environ,DATABASE_URL=database_url,APP_VALIDATION_MODE="1",AUTO_UNIVERSE_SYNC_ON_STARTUP="0",
               AUTO_TRADE_CALENDAR_REFRESH="0",PYTHONIOENCODING="utf-8",
               FACTOR_WAREHOUSE_PATH=str(tmp_path/"unused-factors.duckdb"))
    evidence = ROOT/"tmp/validation-runtime-isolated-ui"
    evidence.mkdir(exist_ok=True)
    folder = evidence/str(time.time_ns())
    folder.mkdir()
    with (folder/"server.log").open("w",encoding="utf-8") as log:
        backend = subprocess.Popen([sys.executable,"-m","uvicorn","app.main:app","--host","127.0.0.1",
            "--port",str(port)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        try:
            client = build_opener(ProxyHandler({}))
            deadline = time.monotonic()+50
            while time.monotonic()<deadline:
                assert backend.poll() is None, (folder/"server.log").read_text(encoding="utf-8")
                try:
                    with client.open(origin+"/health",timeout=2) as response:
                        if response.status==200:
                            break
                except OSError:
                    pass
                time.sleep(.25)
            else:
                pytest.fail("Isolated backend readiness timed out")
            result = subprocess.run([sys.executable,"scripts/verify_market_signals_runtime_ui.py",
                "--origin",origin,"--evidence-dir",str(folder)],cwd=ROOT,env=env,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            (folder/"runner.log").write_text(result.stdout,encoding="utf-8")
            assert result.returncode==0, result.stdout
            report = json.loads((folder/"ui-result.json").read_text(encoding="utf-8"))
            assert report["passed"] and len(report["rules"])==2
        finally:
            if backend.poll() is None:
                backend.terminate()
                backend.wait(timeout=15)
