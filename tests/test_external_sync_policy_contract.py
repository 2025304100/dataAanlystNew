"""Public sync-policy contracts: exact ranges, no overwrites, finality and audit."""
from concurrent.futures import Future
from datetime import date, datetime, timedelta
import json
import re
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from app.models.etf_indicator import EtfIndicator
from app.models.financial_report import StockFinancialReport
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services import etf_basic_data as etf, etf_provider as ep
from app.services import external_data_sync_task as tasks, fundamental_data as fundamental
from app.services import sync_coverage as ledger, valuation_provider as vp
from app.services.factors.store import close_owned_warehouse_path
from tests.test_external_dataset_sync_contract import environment, report
from tests.test_etf_sync_contract import TODAY, Today

pytestmark = pytest.mark.whitebox


class InlineExecutor:
    """Exercise real worker sessions deterministically, without dispatch threads."""
    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, fn, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


@pytest.fixture
def policy_environment(environment, db_session, monkeypatch):
    client, stocks, warehouse = environment
    for module in (etf, fundamental, ledger):
        monkeypatch.setattr(module, "date", Today)
    monkeypatch.setattr(tasks, "ThreadPoolExecutor", InlineExecutor)
    funds = [Symbol(symbol="510300", name="ETF", market="sh", asset_type="etf")]
    db_session.add_all(funds)
    db_session.commit()
    try:
        yield client, stocks, funds, warehouse
    finally:
        close_owned_warehouse_path(warehouse.path)


def create_task(client, dataset, policy="normal", **kwargs):
    payload = {"dataset": dataset, "source": "all", "mode": "incremental",
               "sync_policy": policy, "lookback_days": 1, "include_northbound": False,
               "max_workers": 1, **kwargs}
    preview = client.post("/api/v1/external-data/sync-plans/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["sync_policy"] == policy
    response = client.post("/api/v1/external-data/sync-tasks", json=payload)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["batch_recovery"]["plan"]["sync_policy"] == policy
    return task


def execute(client, task, expected="done"):
    payload = json.loads(task["payload_json"])
    tasks._run_external_sync_task(task["id"], payload["dataset"], payload)
    task = client.get(f"/api/v1/external-data/sync-tasks/{task['id']}").json()
    assert task["status"] == expected, task
    return task


def etf_http(monkeypatch, *, hour=15, nav=1., close=.99, shares=1000.):
    calls = []
    def get(url, *, params, **kwargs):
        assert url == ep.URL
        calls.append(params)
        codes = [secid.split(".")[1] for secid in params["secids"].split(",")]
        stamp = datetime(TODAY.year, TODAY.month, TODAY.day, hour, 1,
                         tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        rows = [{"f12": code, "f2": close, "f441": nav, "f38": shares, "f402": 1.,
                 "f297": int(TODAY.strftime("%Y%m%d")), "f124": stamp} for code in codes]
        return Mock(json=lambda: {"rc": 0, "data": {"total": len(rows), "diff": rows}})
    monkeypatch.setattr(ep.requests, "get", get)
    return calls


def valuation_http(monkeypatch, days):
    calls = []
    def get(url, *, params, **kwargs):
        assert url == vp.URL
        calls.append(params)
        codes = re.findall(r'"(\d{6})"', params["filter"])
        bounds = [date.fromisoformat(value) for value in re.findall(r"TRADE_DATE[<>=]*'([\d-]+)'", params["filter"])]
        start, end = min(bounds), max(bounds)
        rows = [{"SECURITY_CODE": code, "TRADE_DATE": str(day), "PE_TTM": 20., "PB_MRQ": 2.,
                 "TOTAL_MARKET_CAP": 200., "NOTLIMITED_MARKETCAP_A": 160.}
                for code in codes for day in days if start <= day <= end]
        return Mock(json=lambda: {"success": True, "result": {
            "count": len(rows), "pages": 1 if rows else 0, "data": rows}})
    monkeypatch.setattr(vp.requests, "get", get)
    return calls


def insert_valuation(db, symbol, day, **values):
    row = StockValuation(symbol_id=symbol.id, trade_date=day, pe_ttm=10., pb=1.,
        total_market_cap=100., circulating_market_cap=80., source="akshare:stock_value_em",
        raw_json=json.dumps({"_daily_complete": True}))
    for name, value in values.items():
        setattr(row, name, value)
    db.add(row)
    return row


def test_closed_etf_repeated_normal_has_zero_http_but_force_refreshes(policy_environment, db_session, monkeypatch):
    client, _, funds, warehouse = policy_environment
    calls = etf_http(monkeypatch)
    first = execute(client, create_task(client, "etf"))
    assert first["result"]["records"] == 1 and len(calls) == 1
    second = execute(client, create_task(client, "etf"))
    assert second["result"]["skip_reason"] == "already_current" and len(calls) == 1
    forced = execute(client, create_task(client, "etf", "force"))
    assert forced["result"]["records"] == 1 and len(calls) == 2
    summary = client.get("/api/v1/external-data/sync-coverage").json()
    item = next(item for item in summary["datasets"] if item["dataset"] == "etf")
    assert item["earliest_date"] == item["latest_date"] == str(TODAY)
    assert item["covered_points"] == item["completed_points"] == 1
    assert item["first_synced_at"] <= item["last_synced_at"]
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_etf_indicators").fetchone()[0] == 1


def test_intraday_etf_cannot_suppress_close_request(policy_environment, db_session, monkeypatch):
    client, _, funds, _ = policy_environment
    intraday_calls = etf_http(monkeypatch, hour=14)
    execute(client, create_task(client, "etf"))
    assert len(intraday_calls) == 1
    assert ledger.covered_dates(db_session, "etf", [funds[0].id], TODAY, TODAY)[funds[0].id] == set()
    close_calls = etf_http(monkeypatch, hour=15, close=1.02)
    execute(client, create_task(client, "etf"))
    db_session.expire_all()
    assert len(close_calls) == 1
    assert db_session.query(EtfIndicator).one().close == 1.02
    assert ledger.covered_dates(db_session, "etf", [funds[0].id], TODAY, TODAY)[funds[0].id] == {TODAY}


def test_partial_scope_requests_only_uncovered_etf_and_retains_skip_counts(policy_environment, db_session, monkeypatch):
    client, _, funds, _ = policy_environment
    etf_http(monkeypatch)
    execute(client, create_task(client, "etf"))
    db_session.add(Symbol(symbol="159915", name="new ETF", market="sz", asset_type="etf"))
    db_session.commit()
    calls = etf_http(monkeypatch)
    task = execute(client, create_task(client, "etf"))
    assert len(calls) == 1 and calls[0]["secids"] == "0.159915"
    assert task["result"]["total"] == 2
    assert task["result"]["success"] == 1 and task["result"]["skipped"] == 1


def test_missing_etf_preserves_existing_values_and_fills_null_metrics(policy_environment, db_session, monkeypatch):
    client, _, funds, _ = policy_environment
    db_session.add(EtfIndicator(symbol_id=funds[0].id, trade_date=TODAY,
        nav=2., close=2.2, premium_discount=7., fund_size=None, total_shares=None,
        raw_json='{"original":true}', source="retained"))
    db_session.commit()
    calls = etf_http(monkeypatch, nav=1., close=.99, shares=1000.)
    task = execute(client, create_task(client, "etf", "missing"))
    db_session.expire_all()
    row = db_session.query(EtfIndicator).one()
    assert len(calls) == task["result"]["records"] == 1
    assert (row.nav, row.close, row.premium_discount) == (2., 2.2, 7.)
    assert row.total_shares == 1000. and row.fund_size == 2000.
    execute(client, create_task(client, "etf", "missing"))
    assert len(calls) == 1, "fully present metrics should not be requested by missing-only"


def test_force_valuation_bypasses_old_complete_daily_shortcut(policy_environment, db_session, monkeypatch):
    client, stocks, _, _ = policy_environment
    for symbol in stocks:
        insert_valuation(db_session, symbol, TODAY)
    db_session.commit()
    calls = valuation_http(monkeypatch, [TODAY])
    normal = execute(client, create_task(client, "fundamental"))
    assert normal["result"]["skip_reason"] == "already_current" and calls == []
    forced = execute(client, create_task(client, "fundamental", "force"))
    db_session.expire_all()
    assert len(calls) == 1 and forced["result"]["records"] == 2
    assert {row.pe_ttm for row in db_session.query(StockValuation)} == {20.}


def test_closed_capital_flow_normal_skips_http_but_force_refreshes(policy_environment, db_session, monkeypatch):
    from tests.test_external_dataset_sync_contract import install_http, quote
    from app.models.capital_flow import CapitalFlow
    client, stocks, _, _ = policy_environment
    stamp = datetime(TODAY.year, TODAY.month, TODAY.day, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    rows = [{**quote(symbol.symbol), "f124": stamp} for symbol in stocks]
    calls = install_http(monkeypatch, "capital_flow", rows)
    first = execute(client, create_task(client, "capital_flow"))
    assert first["result"]["records"] == 2 and len(calls) == 1
    assert execute(client, create_task(client, "capital_flow"))["result"]["skip_reason"] == "already_current"
    assert len(calls) == 1
    for row in rows:
        row["f62"] = 200.
    execute(client, create_task(client, "capital_flow", "force"))
    db_session.expire_all()
    assert len(calls) == 2
    assert {row.main_net_inflow for row in db_session.query(CapitalFlow)} == {200.}


def test_failed_http_does_not_record_completed_source_coverage(policy_environment, db_session, monkeypatch):
    client, _, funds, _ = policy_environment
    get = Mock(side_effect=ConnectionError("isolated provider outage"))
    monkeypatch.setattr(ep.requests, "get", get)
    task = execute(client, create_task(client, "etf", "force"), expected="failed")
    assert get.call_count == 2 and db_session.query(EtfIndicator).count() == 0
    assert ledger.covered_dates(db_session, "etf", [funds[0].id], TODAY, TODAY)[funds[0].id] == set()
    summary = client.get("/api/v1/external-data/sync-coverage").json()
    assert not any(row["dataset"] == "etf" for row in summary["datasets"])
    assert task["started_at"] and task["finished_at"]


def test_retry_retains_frozen_missing_policy_and_requested_dates(policy_environment, monkeypatch):
    client, _, _, _ = policy_environment
    monkeypatch.setattr(ep.requests, "get", Mock(side_effect=ConnectionError("isolated provider outage")))
    failed = execute(client, create_task(client, "etf", "missing"), expected="failed")
    response = client.post(f"/api/v1/external-data/sync-tasks/{failed['id']}/retry")
    assert response.status_code == 200, response.text
    retry = response.json()
    frozen = retry["batch_recovery"]["plan"]
    assert frozen["sync_policy"] == "missing"
    assert frozen["requested_start_date"] == frozen["requested_end_date"] == str(TODAY)
    assert json.loads(retry["payload_json"])["sync_policy"] == "missing"
    calls = etf_http(monkeypatch)
    finished = execute(client, retry)
    assert len(calls) == 1 and finished["result"]["plan"]["sync_policy"] == "missing"
    details = client.get(f"/api/v1/external-data/sync-tasks/{retry['id']}/partitions").json()
    assert details["plan"]["sync_policy"] == "missing"


def test_historical_normal_requests_only_interior_valuation_hole(policy_environment, db_session, monkeypatch):
    client, stocks, _, _ = policy_environment
    days = [date(2026, 10, 12) + timedelta(days=offset) for offset in range(5)]
    hole = days[2]
    for symbol in stocks:
        for day in days:
            if symbol.id != stocks[0].id or day != hole:
                insert_valuation(db_session, symbol, day)
    db_session.commit()
    calls = valuation_http(monkeypatch, days)
    task = execute(client, create_task(client, "fundamental", "normal", mode="backfill",
        start_date=str(days[0]), end_date=str(days[-1])))
    assert len(calls) == task["result"]["records"] == 1
    assert calls[0]["filter"] == f'(TRADE_DATE=\'{hole}\')(SECURITY_CODE in ("600000"))'
    db_session.expire_all()
    assert db_session.query(StockValuation).count() == 10


def test_gap_repair_preserves_exact_sparse_selection_and_missing_policy(policy_environment, db_session, monkeypatch):
    client, stocks, _, _ = policy_environment
    days = [date(2026, 10, 12) + timedelta(days=offset) for offset in range(3)]
    calls = valuation_http(monkeypatch, days)
    payload = {"dataset": "fundamental", "start_date": str(days[0]), "end_date": str(days[-1]),
        "sync_policy": "missing", "gaps": [{"symbol_id": stocks[0].id, "symbol": stocks[0].symbol,
                                               "trade_date": str(day)} for day in (days[0], days[-1])]}
    response = client.post("/api/v1/external-data/gaps/repair", json=payload)
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["batch_recovery"]["plan"]["sync_policy"] == "missing"
    execute(client, created)
    db_session.expire_all()
    assert {row.trade_date for row in db_session.query(StockValuation)} == {days[0], days[-1]}
    assert len(calls) == 2
    assert all("TRADE_DATE='2026-10-13'" not in call["filter"] for call in calls)


def test_missing_financial_keeps_old_pit_version_and_adds_new_version(policy_environment, db_session, monkeypatch):
    client, stocks, _, _ = policy_environment
    from tests.test_external_dataset_sync_contract import install_http
    from app.services import financial_data as finance
    old_day, new_day = date(2026, 10, 14), date(2026, 10, 15)
    install_http(monkeypatch, "financial", [report(day=old_day, roe=4.)])
    finance.sync_financial_announcements(db_session, stocks, old_day, old_day)
    db_session.commit()
    old = db_session.query(StockFinancialReport).one()
    old_id, old_raw = old.id, old.raw_json
    calls = install_http(monkeypatch, "financial", [report(day=old_day, roe=99.), report(day=new_day, roe=5.)])
    # Historical workers request one symbol at a time; return only that symbol's records.
    original = __import__("app.services.financial_provider", fromlist=["requests"]).requests.get
    def filtered_get(url, *, params, **kwargs):
        response = original(url, params=params, **kwargs)
        payload = response.json()
        rows = payload["result"]["data"] if '"600000.SH"' in params["filter"] else []
        return Mock(json=lambda: {"success": True, "result": {"data": rows,
            "count": len(rows), "pages": 1 if rows else 0}})
    monkeypatch.setattr(vp.requests, "get", filtered_get)
    execute(client, create_task(client, "financial", "missing", mode="backfill",
        start_date=str(old_day), end_date=str(new_day)))
    db_session.expire_all()
    assert db_session.get(StockFinancialReport, old_id).roe_ttm == 4.
    assert db_session.get(StockFinancialReport, old_id).raw_json == old_raw
    assert {(row.announcement_date, row.roe_ttm) for row in db_session.query(StockFinancialReport)} == {(old_day, 4.), (new_day, 5.)}
    assert len(calls) == 2


@pytest.mark.parametrize("policy", ["normal", "missing", "force"])
def test_task_and_persisted_plan_record_frozen_policy_and_execution_times(policy_environment, db_session, monkeypatch, policy):
    client, _, _, _ = policy_environment
    etf_http(monkeypatch)
    # Seed complete data so normal/missing exercise a no-op execution.
    execute(client, create_task(client, "etf", "force"))
    created = create_task(client, "etf", policy)
    finished = execute(client, created)
    assert finished["result"]["plan"]["sync_policy"] == policy
    assert finished["started_at"] and finished["finished_at"]
    assert finished["started_at"] <= finished["finished_at"]
    details = client.get(f"/api/v1/external-data/sync-tasks/{created['id']}/partitions").json()
    plan = details["plan"]
    assert plan["sync_policy"] == policy
    assert plan["requested_start_date"] == plan["requested_end_date"] == str(TODAY)
    assert plan["started_at"] and plan["finished_at"] and plan["started_at"] <= plan["finished_at"]


def test_invalid_sync_policy_is_rejected_before_dispatch(policy_environment):
    client, _, _, _ = policy_environment
    response = client.post("/api/v1/external-data/sync-tasks", json={
        "dataset": "etf", "source": "all", "sync_policy": "overwrite_everything"})
    assert response.status_code == 422
