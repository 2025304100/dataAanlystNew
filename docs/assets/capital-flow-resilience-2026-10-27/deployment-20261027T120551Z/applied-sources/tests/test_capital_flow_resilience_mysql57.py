"""Public async sync API -> local HTTP supplier -> real isolated MySQL 5.7."""
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.manager import DatabaseManager
from app.models.market_data import MarketCalendar
from app.models.symbol import Symbol
from app.models.factor_runtime import FactorSystemConfig
from app.services import capital_flow_history_provider as provider
from app.api.routes.external_data import router

pytestmark = pytest.mark.whitebox


@pytest.fixture
def environment(tmp_path, monkeypatch, record_property, request):
    configured = os.environ.get("CAPITAL_FLOW_RESILIENCE_MYSQL57_URL")
    if not configured:
        pytest.skip("Use the isolated resilience verification driver")
    url = make_url(configured)
    assert url.host in {"localhost", "127.0.0.1"} and not url.database
    name = "codex_cap_recovery_"+uuid4().hex[:16]
    admin = create_engine(url, hide_parameters=True, pool_pre_ping=True)
    with admin.begin() as connection:
        version = connection.scalar(text("SELECT VERSION()"))
        assert version.startswith("5.7.")
        connection.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    target = url.set(database=name)
    manager = DatabaseManager.get()
    manager.initialize(target.render_as_string(hide_password=False), "mysql")
    engine = manager.engine
    Base.metadata.create_all(engine)
    record_property("mysql_version", version)
    record_property("isolated_database", name)
    day = date.today()-timedelta(days=1)
    while day.weekday()>4:
        day -= timedelta(days=1)
    with Session(engine) as db:
        db.add_all([Symbol(symbol="600000",name="恢复验收",asset_type="stock",market="sh"),
                    Symbol(symbol="000001",name="后续股票",asset_type="stock",market="sz")])
        db.add(MarketCalendar(trade_date=day,is_trading_day=1))
        db.add(FactorSystemConfig(id=1,feature_enabled=0,warehouse_path=str(tmp_path/"local.duckdb"),updated_by="test"))
        db.commit()
    monkeypatch.setenv("CAPITAL_FLOW_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CAPITAL_FLOW_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("CAPITAL_FLOW_BACKOFF_MAX_SECONDS", "0")
    requests_seen=[]
    worker_connection={}
    killed=[]
    fault=getattr(request,"param",{"phase":"supplier","count":1})
    def disconnect(identity):
        with admin.begin() as connection:
            selected=connection.scalar(text("SELECT DB FROM information_schema.PROCESSLIST WHERE ID=:id"),{"id":identity})
            assert selected==name, "Only the disposable test worker may be disconnected"
            connection.exec_driver_sql(f"KILL CONNECTION {identity}")
        killed.append(identity)
    if fault.get("rollback_error"):
        from sqlalchemy.exc import OperationalError
        original_rollback=Session.rollback
        injected=[]
        def rollback(session):
            if (killed and not injected and threading.current_thread().name.startswith("external-sync-")
                    and session.get_bind().url.database==name):
                injected.append(True)
                raise OperationalError("ROLLBACK",{},Exception("simulated disconnect during rollback"),connection_invalidated=True)
            return original_rollback(session)
        monkeypatch.setattr(Session,"rollback",rollback)
    def observe(connection, cursor, statement, parameters, context, many):
        if threading.current_thread().name.startswith("external-sync-"):
            worker_connection["id"]=connection.connection.driver_connection.thread_id()
            if (fault["phase"]=="insert" and statement.lower().startswith("insert into capital_flows")
                    and parameters.get("symbol_id")==1 and len(killed)<fault["count"]):
                disconnect(worker_connection["id"])
    event.listen(engine,"before_cursor_execute",observe)
    class Supplier(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs,urlsplit
            values=parse_qs(urlsplit(self.path).query)
            code=values["secid"][0].split(".")[1]
            requests_seen.append(code)
            if code=="600000" and fault["phase"]=="supplier" and not killed:
                identity=worker_connection["id"]
                disconnect(identity)
            body={"rc":0,"data":{"code":code,"market":1 if code.startswith("6") else 0,
                "klines":[f"{day},100,-20,-80,40,60,1,-.2,-.8,.4,.6,10,1,0,0"]}}
            content=json.dumps(body).encode()
            status=503 if code=="600000" and (fault.get("persistent_503") or
                fault.get("transient_503") and requests_seen.count(code)==1) else 200
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Supplier)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    origin=f"http://127.0.0.1:{server.server_port}/history"
    monkeypatch.setattr(provider,"URL",origin)
    monkeypatch.setattr(provider,"FALLBACK_URL",origin)
    app=FastAPI()
    app.include_router(router,prefix="/api/v1")
    try:
        with TestClient(app) as client:
            yield client, day, requests_seen, killed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        event.remove(engine,"before_cursor_execute",observe)
        manager.dispose()
        admin.dispose()  # Keep the unique schema for evidence; never drop an existing DB.


def start_and_wait(client,day):
    created=client.post("/api/v1/external-data/sync-tasks",json={"dataset":"capital_flow","mode":"backfill",
        "source":"all","start_date":str(day),"end_date":str(day),"include_northbound":False,"sync_policy":"missing"})
    assert created.status_code==200, created.text
    identity=created.json()["id"]
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        response=client.get(f"/api/v1/external-data/sync-tasks/{identity}")
        assert response.status_code==200, response.text
        result=response.json()
        if result["status"] in {"done","failed","cancelled"}:
            break
        time.sleep(.05)
    else:
        pytest.fail("The two-symbol isolated sync did not terminate")
    return identity,result


@pytest.mark.parametrize("environment",[{"phase":"supplier","count":1},{"phase":"insert","count":1},
    {"phase":"supplier","count":1,"rollback_error":True}],indirect=True,
                         ids=["supplier_wait_disconnect","write_disconnect","rollback_disconnect"])
def test_idle_disconnect_keeps_fetched_history_and_continues_next_symbol(environment):
    client,day,requests_seen,killed=environment
    identity,result=start_and_wait(client,day)
    assert result["status"]=="done", result
    assert result["ok_count"]==2 and result["failed_count"]==0
    parts=client.get(f"/api/v1/external-data/sync-tasks/{identity}/partitions").json()
    assert sorted((row["symbol"],row["status"],row["rows_written"]) for row in parts["partitions"])==[
        ("000001","done",1),("600000","done",1)]
    assert len(killed)==1 and sorted(requests_seen)==["000001","600000"]
    assert result["result"]["database_recoveries"]==1
    _,second=start_and_wait(client,day)
    assert second["status"]=="done" and second["result"]["skipped"]==2
    assert sorted(requests_seen)==["000001","600000"], "Missing-only replay must not fetch covered stocks"


@pytest.mark.parametrize("environment",[{"phase":"insert","count":2}],indirect=True)
def test_repeated_database_disconnect_is_bounded_and_retry_targets_only_failed_stock(environment):
    client,day,requests_seen,killed=environment
    identity,result=start_and_wait(client,day)
    assert result["status"]=="failed" and result["failed_count"]==1 and result["ok_count"]==1
    assert len(killed)==2 and sorted(requests_seen)==["000001","600000"]
    retried=client.post(f"/api/v1/external-data/sync-tasks/{identity}/retry")
    assert retried.status_code==200,retried.text
    until=time.monotonic()+20
    while time.monotonic()<until:
        final=client.get('/api/v1/external-data/sync-tasks/'+retried.json()["id"]).json()
        if final["status"] in {"done","failed","cancelled"}:break
        time.sleep(.05)
    assert final["status"]=="done",final
    assert requests_seen.count("600000")==2 and requests_seen.count("000001")==1


@pytest.mark.parametrize("environment",[{"phase":"supplier","count":1,"transient_503":True}],indirect=True)
def test_real_http_retry_and_database_recovery_share_the_same_fetched_result(environment):
    client,day,requests_seen,killed=environment
    identity,result=start_and_wait(client,day)
    assert result["status"]=="done" and result["ok_count"]==2 and result["failed_count"]==0
    assert result["result"]["database_recoveries"]==1 and len(killed)==1
    assert requests_seen.count("600000")==2 and requests_seen.count("000001")==1


@pytest.mark.parametrize("environment",[{"phase":"supplier","count":1,"persistent_503":True,"rollback_error":True}],indirect=True)
def test_supplier_failure_and_rollback_disconnect_do_not_abort_remaining_stock(environment):
    client,day,requests_seen,killed=environment
    identity,result=start_and_wait(client,day)
    assert result["status"]=="failed" and result["failed_count"]==1 and result["ok_count"]==1, result
    parts=client.get(f"/api/v1/external-data/sync-tasks/{identity}/partitions").json()
    assert sorted((item["symbol"],item["status"]) for item in parts["partitions"])==[
        ("000001","done"),("600000","failed")]
    assert requests_seen.count("600000")==3 and requests_seen.count("000001")==1
