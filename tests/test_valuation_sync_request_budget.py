"""Public incremental valuation tasks must not amplify market-wide requests."""
from collections import Counter
from datetime import date, datetime, timedelta
import time
import re
from unittest.mock import Mock

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import external_data
from app.db.session import get_db
from app.models.factor_runtime import FactorSystemConfig
from app.models.symbol import Symbol
from app.models.market_data import MarketCalendar
from app.models.stock_valuation import StockValuation
from app.models.watchlist import Watchlist, WatchlistItem
from app.services import fundamental_data, external_data_sync_task as service
from app.services.factors.store import FactorWarehouse
from app.services import valuation_provider

TODAY = date(2026, 10, 16)


class DateMeta(type):
    def __instancecheck__(cls, value):
        return isinstance(value, date)


class TradingDate(date, metaclass=DateMeta):
    @classmethod
    def today(cls):
        return TODAY


def _mock_daily(monkeypatch, market):
    def get(url, *, params, **kwargs):
        frame = market()
        wanted = set(re.findall(r'"(\d{6})"', params["filter"]))
        rows = []
        for _, row in frame.iterrows():
            code = str(row.get("代码", "600001"))
            if code in wanted:
                rows.append({"SECURITY_CODE": code, "TRADE_DATE": str(row.get("数据日期", TODAY)),
                    "PE_TTM": row.get("PE(TTM)", row.get("市盈率-动态", 12.)),
                    "PB_MRQ": row.get("市净率", 1.2), "TOTAL_MARKET_CAP": row.get("总市值", 100.),
                    "NOTLIMITED_MARKETCAP_A": row.get("流通市值", 80.)})
        return Mock(json=lambda: {"success": True,
            "result": {"data": rows, "count": len(rows), "pages": 1 if rows else 0}})
    monkeypatch.setattr(valuation_provider.requests, "get", get)
    return get


@pytest.fixture
def valuation_client(db_session, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("daily valuation must never call unbounded historical or full-market APIs")

    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", forbidden)
    monkeypatch.setattr(fundamental_data.ak, "stock_zh_a_spot_em", forbidden)
    monkeypatch.setattr(service, "date", TradingDate)
    monkeypatch.setattr(fundamental_data, "date", TradingDate)
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secret.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    warehouse_path = tmp_path / "valuation.duckdb"
    FactorWarehouse(warehouse_path).initialize()
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1, warehouse_path=str(warehouse_path), updated_by="test"))
    db_session.add_all([
        Symbol(symbol="600001", name="One", asset_type="stock", market="sh", industry="Bank"),
        Symbol(symbol="600002", name="Two", asset_type="stock", market="sh", industry=None),
    ])
    db_session.commit()
    db_session.add(MarketCalendar(trade_date=TODAY, is_trading_day=1))
    watchlist = Watchlist(name="valuation-test", list_type="manual")
    db_session.add(watchlist)
    db_session.flush()
    for symbol in db_session.query(Symbol).all():
        db_session.add(WatchlistItem(watchlist_id=watchlist.id, symbol_id=symbol.id))
    db_session.commit()
    app = FastAPI()
    app.include_router(external_data.router, prefix="/api/v1")
    monkeypatch.setattr(external_data, "date", TradingDate)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def _run(client, source="all", dataset="fundamental", include_northbound=False, **options):
    created = client.post("/api/v1/external-data/sync-tasks", json={
        "dataset": dataset, "source": source, "mode": "incremental",
        "lookback_days": 1, "include_northbound": include_northbound, "max_workers": 1,
        **options,
    })
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/external-data/sync-tasks/{task_id}").json()
        if task["status"] in {"done", "failed", "cancelled"}:
            return task
        time.sleep(.1)
    pytest.fail("isolated valuation task did not finish")


def test_daily_failure_never_falls_back_even_for_small_watchlist(valuation_client, monkeypatch):
    calls = Counter()

    def market():
        calls["market"] += 1
        raise ConnectionError("isolated unavailable snapshot")

    def history(symbol):
        calls["history"] += 1
        return pd.DataFrame([{"数据日期": TODAY, "PE(TTM)": 12.,
            "市净率": 1.2, "总市值": 100., "流通市值": 80.}])

    def info(symbol):
        calls["industry"] += 1
        raise ConnectionError("unnecessary industry request")

    _mock_daily(monkeypatch, market)
    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", history)
    monkeypatch.setattr(fundamental_data.ak, "stock_individual_info_em", info)
    task = _run(valuation_client, source="watchlist")
    assert task["status"] == "failed", task
    assert task["ok_count"] == 0
    assert calls["market"] == 2
    assert calls["history"] == 0
    assert calls["industry"] == 0
    assert "未切换历史接口" in task["message"]


def test_missing_requested_day_is_failed_instead_of_writing_old_valuation(valuation_client, monkeypatch, db_session):
    def market():
        return pd.DataFrame([{"代码": "600001", "数据日期": TODAY - timedelta(days=1),
            "PE(TTM)": 12., "市净率": 1.2, "总市值": 100.}])

    def history(symbol):
        return pd.DataFrame([{"数据日期": TODAY - timedelta(days=1),
            "PE(TTM)": 12., "市净率": 1.2, "总市值": 100., "流通市值": 80.}])

    _mock_daily(monkeypatch, market)
    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", history)
    task = _run(valuation_client, source="watchlist")
    assert task["status"] == "failed", task
    assert task["ok_count"] == 0
    assert task["failed_count"] > 0
    assert db_session.query(StockValuation).count() == 0
    assert "范围外" in task["message"]


def test_healthy_market_snapshot_keeps_constant_request_budget(valuation_client, monkeypatch):
    calls = Counter()

    def market():
        calls["market"] += 1
        return pd.DataFrame([
            {"代码": "600001", "市盈率-动态": 12., "市净率": 1.2, "总市值": 100.},
            {"代码": "600002", "市盈率-动态": 13., "市净率": 1.3, "总市值": 120.},
        ])

    def forbidden(*_args, **_kwargs):
        calls["extra"] += 1
        raise AssertionError("healthy batch snapshot must not fan out")

    _mock_daily(monkeypatch, market)
    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", forbidden)
    monkeypatch.setattr(fundamental_data.ak, "stock_individual_info_em", forbidden)
    task = _run(valuation_client)
    assert task["status"] == "done", task
    assert task["ok_count"] == 2
    assert calls == {"market": 1}


@pytest.mark.parametrize("dataset", ["fundamental", "capital_flow", "etf", "lhb", "hot_rank", "tail_proxy"])
@pytest.mark.parametrize("day", [date(2026, 10, 17), date(2026, 10, 18), date(2026, 10, 1)])
def test_closed_market_skips_without_provider_or_mirror(valuation_client, db_session, monkeypatch, day, dataset):
    class ClosedDate(TradingDate):
        @classmethod
        def today(cls):
            return day

    monkeypatch.setattr(service, "date", ClosedDate)
    db_session.add(MarketCalendar(trade_date=day, is_trading_day=0))
    db_session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("closed market must not fetch providers or mirror warehouse")

    monkeypatch.setattr(fundamental_data.ak, "stock_zh_a_spot_em", forbidden)
    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", forbidden)
    monkeypatch.setattr(service, "_mirror_external_factor_inputs", forbidden)
    monkeypatch.setattr(service, "resolve_external_symbols", forbidden)
    monkeypatch.setattr(valuation_provider.requests, "get", forbidden)
    task = _run(valuation_client, source="candidates" if dataset == "tail_proxy" else "all",
                dataset=dataset, include_northbound=dataset == "capital_flow")
    assert task["status"] == "done", task
    assert task["failed_count"] == 0
    assert task["result"]["skip_reason"] == "market_closed"
    assert task["result"]["records"] == 0
    assert "休市" in task["message"]
    details = service.get_external_sync_partitions(task["id"])
    assert details["plan"]["status"] == "done"
    assert details["partitions"] == []
    assert all(p["status"] == "skipped" and p["attempts"] == 0 for p in details["partitions"])


def test_all_scope_snapshot_failure_never_fans_out(valuation_client, monkeypatch):
    calls = Counter()

    def market():
        calls["market"] += 1
        raise ConnectionError("batch unavailable")

    def history(*args, **kwargs):
        calls["history"] += 1
        raise AssertionError("must not fan out")

    _mock_daily(monkeypatch, market)
    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", history)
    task = _run(valuation_client)
    assert task["status"] == "failed", task
    assert calls == {"market": 2}
    assert "同步已停止" in task["message"]


def test_backfill_of_only_holidays_never_downloads_stock_history(valuation_client, db_session, monkeypatch):
    db_session.add_all([MarketCalendar(trade_date=date(2026, 10, day), is_trading_day=0)
        for day in range(1, 8)])
    db_session.commit()
    calls = Counter()

    def history(*args, **kwargs):
        calls["history"] += 1
        raise AssertionError("holiday-only backfill must not fetch stock history")

    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", history)
    task = _run(valuation_client, mode="backfill", start_date="2026-10-01", end_date="2026-10-07")
    assert task["status"] == "done", task
    assert task["result"]["skip_reason"] == "market_closed"
    assert not calls


def test_current_local_valuation_skips_provider(valuation_client, db_session, monkeypatch):
    for symbol in db_session.query(Symbol).all():
        db_session.add(StockValuation(symbol_id=symbol.id, trade_date=TODAY, pb=1.2, total_market_cap=100))
    db_session.commit()

    def forbidden():
        pytest.fail("current local data must not be downloaded again")

    _mock_daily(monkeypatch, forbidden)
    task = _run(valuation_client)
    assert task["status"] == "done", task
    assert task["result"]["skip_reason"] == "already_current"
    assert service.get_external_sync_partitions(task["id"])["partitions"] == []


def test_intraday_local_valuation_does_not_suppress_closing_refresh(valuation_client, db_session, monkeypatch):
    for symbol in db_session.query(Symbol).all():
        db_session.add(StockValuation(symbol_id=symbol.id, trade_date=TODAY,
            pb=1.2, total_market_cap=100, created_at=datetime(2026, 10, 16, 2)))
    db_session.commit()
    calls = Counter()

    def market():
        calls["market"] += 1
        return pd.DataFrame([
            {"代码": "600001", "市盈率-动态": 12., "市净率": 1.3, "总市值": 110.},
            {"代码": "600002", "市盈率-动态": 13., "市净率": 1.4, "总市值": 120.},
        ])

    _mock_daily(monkeypatch, market)
    task = _run(valuation_client)
    assert task["status"] == "done", task
    assert task["ok_count"] == 2
    assert calls == {"market": 1}
    repeated = _run(valuation_client)
    assert repeated["result"]["skip_reason"] == "already_current"
    assert calls == {"market": 1}


def test_unknown_weekday_calendar_stops_before_fetch(valuation_client, db_session, monkeypatch):
    db_session.query(MarketCalendar).delete()
    db_session.commit()
    calls = Counter()

    def market():
        calls["market"] += 1
        return pd.DataFrame()

    _mock_daily(monkeypatch, market)
    task = _run(valuation_client)
    assert task["status"] == "failed", task
    assert "交易日历缺少" in task["message"]
    assert not calls


def test_direct_snapshot_refuses_closed_day(valuation_client, db_session, monkeypatch):
    db_session.get(MarketCalendar, TODAY).is_trading_day = 0
    db_session.commit()
    calls = Counter()

    def market():
        calls["market"] += 1

    monkeypatch.setattr(fundamental_data.ak, "stock_zh_a_spot_em", market)
    with pytest.raises(ValueError, match="休市日"):
        fundamental_data.sync_market_valuation_snapshot(db_session, db_session.query(Symbol).all(), TODAY)
    assert not calls


def test_partial_local_coverage_downloads_only_missing_codes(valuation_client, db_session, monkeypatch):
    one = db_session.query(Symbol).filter_by(symbol="600001").one()
    db_session.add(StockValuation(symbol_id=one.id, trade_date=TODAY, pb=1, total_market_cap=100))
    db_session.commit()
    get = _mock_daily(monkeypatch, lambda: pd.DataFrame([{"代码": "600002", "PE(TTM)": 20.}]))
    spy = Mock(wraps=get)
    monkeypatch.setattr(valuation_provider.requests, "get", spy)
    task = _run(valuation_client)
    assert task["status"] == "done", task
    assert task["ok_count"] == 1 and task["result"]["skipped"] == 1
    assert task["total"] == task["processed"] == 2
    params = spy.call_args.kwargs["params"]
    assert '"600002"' in params["filter"] and '"600001"' not in params["filter"]
    assert params["pageSize"] == "1"
    assert len(service.get_external_sync_partitions(task["id"])["partitions"]) == 1


def test_unpublished_day_stops_after_one_bounded_request(valuation_client, monkeypatch):
    get = _mock_daily(monkeypatch, lambda: pd.DataFrame())
    spy = Mock(wraps=get)
    monkeypatch.setattr(valuation_provider.requests, "get", spy)
    task = _run(valuation_client)
    assert task["status"] == "failed", task
    assert "尚未发布" in task["message"]
    assert spy.call_count == 1


def test_legacy_sync_endpoint_uses_same_bounded_daily_request(valuation_client, monkeypatch):
    get = _mock_daily(monkeypatch, lambda: pd.DataFrame([
        {"代码": "600001", "PE(TTM)": 12.}, {"代码": "600002", "PE(TTM)": 13.}]))
    spy = Mock(wraps=get)
    monkeypatch.setattr(valuation_provider.requests, "get", spy)
    response = valuation_client.post("/api/v1/external-data/fundamental/sync?source=all")
    assert response.status_code == 200, response.text
    assert response.json()["records"] == 2
    assert spy.call_count == 1
    assert "TRADE_DATE='2026-10-16'" in spy.call_args.kwargs["params"]["filter"]
