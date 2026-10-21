"""Remaining input datasets: request budget, dates, real writes and empty results."""
from datetime import date, datetime, timedelta
import json
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import external_data
from app.db.session import get_db
from app.models.capital_flow import CapitalFlow
from app.models.async_task import AsyncTaskRecord
from app.models.financial_report import StockFinancialReport
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.factor_runtime import FactorSystemConfig
from app.models.market_data import MarketCalendar
from app.models.symbol import Symbol
from app.services import capital_flow_data as flow, capital_flow_provider as fp
from app.services import financial_data as finance, financial_provider as rp
from app.services import hot_rank_data as hot, lhb_data as lhb, external_data_sync_task as tasks
from app.services.factors.store import FactorWarehouse
from tests.test_etf_sync_contract import TODAY, Today

pytestmark = pytest.mark.whitebox


class DeferredThread:
    def __init__(self, **kwargs):
        pass
    def start(self):
        pass


@pytest.fixture
def environment(db_session, tmp_path, monkeypatch):
    for module in (flow, finance, rp, hot, lhb, tasks):
        monkeypatch.setattr(module, "date", Today)
    monkeypatch.setattr(tasks.threading, "Thread", DeferredThread)
    monkeypatch.setattr("app.services.akshare_registry.apply_delay", lambda *a: None)
    def forbidden(*a, **k):
        pytest.fail("unexpected external request")
    monkeypatch.setattr(fp.requests, "get", forbidden)
    monkeypatch.setattr(fp.requests, "post", forbidden)
    for name in ("stock_individual_fund_flow", "stock_individual_fund_flow_rank", "stock_zh_a_spot_em", "stock_value_em"):
        monkeypatch.setattr(flow.ak, name, forbidden)
    symbols = [Symbol(symbol=code, name=code, asset_type="stock", market=market) for code, market in (("600000", "sh"), ("000001", "sz"))]
    db_session.add_all(symbols)
    db_session.add_all([MarketCalendar(trade_date=TODAY, is_trading_day=1),
                       MarketCalendar(trade_date=TODAY-timedelta(days=1), is_trading_day=0),
                       MarketCalendar(trade_date=date(2026, 10, 1), is_trading_day=0)])
    warehouse = FactorWarehouse(tmp_path / "inputs.duckdb")
    warehouse.initialize()
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1, warehouse_path=str(warehouse.path), updated_by="test"))
    db_session.commit()
    app = FastAPI()
    app.include_router(external_data.router, prefix="/api/v1")
    monkeypatch.setattr(external_data, "date", Today)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client, symbols, warehouse


def quote(code, *, day=TODAY, value=100):
    return {"f12": code, "f13": 1 if code.startswith("6") else 0, "f62": value,
            "f184": 1, "f66": 20, "f72": 80, "f78": -10, "f84": -90,
            "f124": datetime(day.year, day.month, day.day, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(),
            "f297": int(day.strftime("%Y%m%d"))}


def report(code="600000.SH", *, day=TODAY, roe=4):
    return {"SECUCODE": code, "NOTICE_DATE": str(day), "REPORT_DATE": "2026-09-30",
            "ROEJQ": roe, "PARENTNETPROFIT": 100, "TOTALOPERATEREVE": 200,
            "PARENTNETPROFITTZ": 10, "TOTALOPERATEREVETZ": 20}


def install_http(monkeypatch, dataset, rows):
    calls = []
    def get(url, *, params, **kwargs):
        calls.append((url, params))
        if dataset == "capital_flow":
            assert url == fp.URL
            payload = {"rc": 0, "data": {"total": len(rows), "diff": rows}}
        else:
            assert url == rp.URL
            payload = {"success": True, "result": {"count": len(rows), "pages": 1 if rows else 0, "data": rows}}
        return Mock(json=lambda: payload)
    monkeypatch.setattr(fp.requests, "get", get)
    return calls


def run_task(client, dataset):
    response = client.post("/api/v1/external-data/sync-tasks", json={"dataset": dataset, "source": "all", "mode": "incremental", "include_northbound": False})
    assert response.status_code == 200, response.text
    task = response.json()
    tasks._run_external_sync_task(task["id"], dataset, json.loads(task["payload_json"]))
    return client.get(f"/api/v1/external-data/sync-tasks/{task['id']}").json()


@pytest.mark.parametrize("dataset", ["capital_flow", "financial"])
def test_daily_public_api_uses_one_selected_request_and_real_warehouse(environment, db_session, monkeypatch, dataset):
    client, symbols, warehouse = environment
    rows = [quote("600000"), quote("000001")] if dataset == "capital_flow" else [report(), report("000001.SZ")]
    calls = install_http(monkeypatch, dataset, rows)
    task = run_task(client, dataset)
    assert task["status"] == "done", task
    result = task["result"]
    assert result["success"] == result["records"] == result["warehouse_mirror"]["records"] == 2
    assert result["quality_snapshot"]["fields"] == 1
    assert result["written_start_date"] == result["written_end_date"] == str(TODAY)
    assert len(calls) == 1
    params = calls[0][1]
    if dataset == "capital_flow":
        assert set(params["secids"].split(",")) == {"1.600000", "0.000001"}
        assert db_session.query(CapitalFlow).count() == 2
        table = "raw_fund_flows"
    else:
        assert all(value in params["filter"] for value in ('"600000.SH"', '"000001.SZ"', f"NOTICE_DATE>='{TODAY}'", f"NOTICE_DATE<='{TODAY}'"))
        assert params["sty"] == rp.COLUMNS
        assert db_session.query(StockFinancialReport).count() == 2
        table = "raw_financial_reports"
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 2


@pytest.mark.parametrize("dataset,reason", [("capital_flow", "data_unavailable"), ("financial", "no_new_data")])
def test_empty_response_skips_mirror_and_quality(environment, monkeypatch, dataset, reason):
    client, _, _ = environment
    calls = install_http(monkeypatch, dataset, [])
    task = run_task(client, dataset)
    assert task["status"] == "done" and task["result"]["records"] == 0
    assert task["result"]["skip_reason"] == reason
    assert task["result"]["warehouse_mirror"]["status"] == "skipped"
    assert task["result"]["quality_snapshot"]["status"] == "skipped"
    assert len(calls) == 1


def test_repeated_financial_reports_are_not_counted_as_new_writes(environment, db_session, monkeypatch):
    _, symbols, _ = environment
    install_http(monkeypatch, "financial", [report()])
    first = finance.sync_financial_announcements(db_session, symbols, TODAY, TODAY)
    db_session.commit()
    second = finance.sync_financial_announcements(db_session, symbols, TODAY, TODAY)
    assert first["records"] == 1 and second["records"] == 0
    assert db_session.query(StockFinancialReport).count() == 1


@pytest.mark.parametrize("bad", ["old_date", "wrong_symbol", "future_period", "duplicate"])
def test_financial_provider_rejects_bad_scope_before_any_write(environment, db_session, monkeypatch, bad):
    _, symbols, _ = environment
    row = report()
    if bad == "old_date": row["NOTICE_DATE"] = "2026-10-16"
    if bad == "wrong_symbol": row["SECUCODE"] = "600999.SH"
    if bad == "future_period": row["REPORT_DATE"] = "2026-12-31"
    rows = [row, row.copy()] if bad == "duplicate" else [row]
    install_http(monkeypatch, "financial", rows)
    with pytest.raises(ValueError):
        finance.sync_financial_announcements(db_session, symbols, TODAY, TODAY)
    assert db_session.query(StockFinancialReport).count() == 0


@pytest.mark.parametrize("bad", ["missing_date", "conflicting_date", "wrong_symbol", "duplicate"])
def test_flow_provider_rejects_bad_scope_before_any_write(environment, db_session, monkeypatch, bad):
    _, symbols, _ = environment
    row = quote("600000")
    if bad == "missing_date": row.pop("f297")
    if bad == "conflicting_date": row["f297"] = 20261016
    if bad == "wrong_symbol": row["f12"] = "600999"
    rows = [row, row.copy()] if bad == "duplicate" else [row]
    install_http(monkeypatch, "capital_flow", rows)
    with pytest.raises(ValueError):
        flow.sync_market_capital_flow_snapshot(db_session, symbols)
    assert db_session.query(CapitalFlow).count() == 0


def test_current_flow_cannot_overwrite_old_session_or_erase_partial_fields(environment, db_session, monkeypatch):
    _, symbols, _ = environment
    db_session.add(CapitalFlow(symbol_id=symbols[0].id, trade_date=TODAY, main_net_inflow=1, super_large_net_inflow=123))
    db_session.commit()
    rows = [quote("600000", day=TODAY-timedelta(days=3)), quote("000001", value="-")]
    install_http(monkeypatch, "capital_flow", rows)
    assert flow.sync_market_capital_flow_snapshot(db_session, symbols) == set()
    rows[0] = quote("600000"); rows[0]["f66"] = "-"
    assert flow.sync_market_capital_flow_snapshot(db_session, symbols) == {symbols[0].id}
    assert db_session.query(CapitalFlow).one().super_large_net_inflow == 123


def test_weekend_and_holiday_stop_legacy_service_calls(environment, db_session, monkeypatch):
    _, symbols, _ = environment
    class Closed(Today):
        @classmethod
        def today(cls): return date(2026, 10, 18)
    monkeypatch.setattr(flow, "date", Closed)
    monkeypatch.setattr(hot, "date", Closed)
    assert flow.sync_market_capital_flow_snapshot(db_session, symbols) == set()
    assert hot.sync_hot_rank_snapshot(db_session).skip_reason == "market_closed"
    assert lhb.sync_lhb_institution_trades(db_session, start_date=date(2026,10,1), end_date=date(2026,10,1)).skip_reason == "market_closed"


def test_hot_rank_rejects_history_and_fetches_rank_without_quotes(environment, db_session, monkeypatch):
    with pytest.raises(ValueError, match="当前快照"):
        hot.sync_hot_rank_snapshot(db_session, as_of=TODAY-timedelta(days=3))
    calls=[]
    def post(url, **kwargs):
        calls.append(url)
        return Mock(json=lambda: {"data": [{"sc": "SH600000", "rk": 1}, {"sc": "SZ000001", "rk": 2}]})
    monkeypatch.setattr(hot.requests, "post", post)
    assert hot.sync_hot_rank_snapshot(db_session).written == 2
    assert len(calls) == 1
    assert db_session.query(StockHotRankSnapshot).count() == 2


def test_lhb_missing_values_remain_missing_and_partial_refresh_preserves_fact(environment, db_session, monkeypatch):
    frame = pd.DataFrame([{"代码":"600000", "上榜日期":str(TODAY), "机构买入净额":10}])
    monkeypatch.setattr(lhb, "call_akshare_with_retry", lambda *a, **k: frame)
    summary=lhb.sync_lhb_institution_trades(db_session, start_date=TODAY, end_date=TODAY)
    row=db_session.query(LhbInstitutionTrade).one()
    assert summary.written == 1 and row.institution_buy is None and row.buyer_institution_count is None
    row.institution_buy=100
    db_session.commit()
    lhb.sync_lhb_institution_trades(db_session, start_date=TODAY, end_date=TODAY)
    assert row.institution_buy == 100


def test_lhb_outside_range_is_not_written(environment, db_session, monkeypatch):
    frame=pd.DataFrame([{"代码":"600000", "上榜日期":"2026-10-16", "机构买入净额":10}])
    monkeypatch.setattr(lhb, "call_akshare_with_retry", lambda *a, **k: frame)
    with pytest.raises(ValueError, match="区间外"):
        lhb.sync_lhb_institution_trades(db_session, start_date=TODAY, end_date=TODAY)
    assert db_session.query(LhbInstitutionTrade).count() == 0


def test_historical_scoring_never_reads_future_flow(environment, db_session, monkeypatch):
    _, symbols, _=environment
    db_session.add(CapitalFlow(symbol_id=symbols[0].id, trade_date=TODAY, main_net_inflow=100, main_net_inflow_score=90))
    db_session.commit()
    monkeypatch.setattr(flow, "sync_symbol_capital_flow", lambda *a: None)
    assert flow.get_or_sync_capital_flow(db_session, symbols[0], TODAY-timedelta(days=10)) is None


@pytest.mark.parametrize("dataset", ["capital_flow", "financial"])
def test_daily_network_failure_has_two_attempts_and_no_history_fallback(environment, monkeypatch, dataset):
    client, _, _ = environment
    calls=[]
    def fail(*a, **k):
        calls.append(1)
        raise ConnectionError("connection reset")
    monkeypatch.setattr(fp.requests, "get", fail)
    task=run_task(client, dataset)
    assert task["status"] == "failed" and len(calls) == 2


@pytest.mark.parametrize("dataset", ["capital_flow", "financial"])
def test_101_symbols_use_two_bounded_batches(environment, db_session, monkeypatch, dataset):
    _, symbols, _ = environment
    extra=[Symbol(symbol=str(600001+i), name="batch", asset_type="stock", market="sh") for i in range(99)]
    db_session.add_all(extra); db_session.flush()
    symbols = symbols + extra
    calls=[]
    if dataset == "capital_flow":
        def fetch(db, selected, **kwargs):
            calls.append(selected)
            return {}
        monkeypatch.setattr(fp, "fetch_capital_flow_quotes", fetch)
        flow.sync_market_capital_flow_snapshot(db_session, symbols)
    else:
        def fetch(db, selected, *args, **kwargs):
            calls.append(selected)
            return []
        monkeypatch.setattr(rp, "fetch_financial_rows", fetch)
        finance.sync_financial_announcements(db_session, symbols, TODAY, TODAY)
    assert [len(batch) for batch in calls] == [100,1]


def test_financial_gap_repair_respects_selected_announcement_range(environment, db_session, monkeypatch):
    _, symbols, _ = environment
    captured=[]
    monkeypatch.setattr(tasks, "start_external_data_sync", lambda dataset, payload: captured.append(payload))
    tasks.start_external_gap_repair("financial", start_date=TODAY-timedelta(days=60), end_date=TODAY,
        gaps=[{"symbol_id": symbols[0].id, "symbol": symbols[0].symbol, "trade_date": TODAY}])
    assert captured[0]["mode"] == "backfill"
    assert captured[0]["sync_policy"] == "missing"
    assert captured[0]["start_date"] == TODAY-timedelta(days=60)
    assert captured[0]["_partition_specs"][0]["start_date"] == TODAY-timedelta(days=60)


@pytest.mark.parametrize("dataset", ["financial", "hot_rank", "lhb"])
def test_old_scheduled_workers_publish_actual_rows_to_configured_warehouse(environment, db_session, monkeypatch, dataset):
    _, _, warehouse=environment
    from app.services import financial_report_task, hot_rank_task, lhb_institution_task
    worker = {"financial": financial_report_task, "hot_rank": hot_rank_task, "lhb": lhb_institution_task}[dataset]
    if hasattr(worker,"date"):
        monkeypatch.setattr(worker,"date",Today)
    if dataset == "financial":
        install_http(monkeypatch,"financial",[report()])
        run=worker._run_financial_report_task
        table="raw_financial_reports"
    elif dataset == "hot_rank":
        monkeypatch.setattr(hot.requests,"post",lambda *a, **k: Mock(json=lambda:{"data":[{"sc":"SH600000","rk":1}]}))
        run=worker._run_hot_rank_task
        table="raw_sentiment"
    else:
        frame=pd.DataFrame([{"代码":"600000","上榜日期":str(TODAY),"机构买入净额":10}])
        monkeypatch.setattr(lhb,"call_akshare_with_retry",lambda *a,**k:frame)
        run=worker._run_lhb_institution_task
        table="raw_sentiment"
    task=AsyncTaskRecord(id=f"legacy-{dataset}",task_type=worker.TASK_TYPE,status="queued",stage="queued",
                         payload_json=json.dumps({"source":"all","limit":20,"lookback_days":3}))
    db_session.add(task); db_session.commit()
    run(task.id)
    db_session.expire_all()
    task=db_session.get(AsyncTaskRecord,task.id)
    assert task.status=="done",task.message
    result=json.loads(task.result_json)
    assert result["warehouse_mirror"]["status"]=="done",result
    assert result["quality_snapshot"]["fields"]==1,result
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]==1
