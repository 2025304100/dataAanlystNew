"""ETF button/API → bounded HTTP → SQL → warehouse → field diagnostics."""
from datetime import date, datetime, timedelta
import json
import time
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import external_data
from app.db.session import get_db
from app.models.daily_bar import DailyBar
from app.models.etf_indicator import EtfIndicator
from app.models.factor_runtime import FactorSystemConfig
from app.models.market_data import MarketCalendar
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services import etf_basic_data as etf, etf_provider as provider, external_data_sync_task as tasks
from app.services.factors.formula_catalog import _query_field_stats, invalidate_formula_catalog_cache
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox
TODAY = date(2026, 10, 19)


class DateMeta(type):
    def __instancecheck__(cls, value):
        return isinstance(value, date)


class Today(date, metaclass=DateMeta):
    @classmethod
    def today(cls):
        return TODAY


@pytest.fixture
def environment(db_session, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("daily ETF synchronization must not download market lists or historical NAV")
    for name in ("fund_etf_spot_em", "fund_etf_fund_daily_em", "fund_etf_fund_info_em"):
        monkeypatch.setattr(etf.ak, name, forbidden)
    monkeypatch.setattr(etf, "date", Today)
    monkeypatch.setattr(tasks, "date", Today)
    monkeypatch.setattr("app.services.akshare_registry.apply_delay", lambda *a: None)
    symbols = [Symbol(symbol=code, name=code, market=market, asset_type=asset) for code, market, asset in (
        ("510300", "sh", "etf"), ("159915", "sz", "etf"), ("513120", "sh", "etf"),
        ("SPY", "us", "etf"), ("000001", "sz", "stock"))]
    db_session.add_all(symbols)
    db_session.add(MarketCalendar(trade_date=TODAY, is_trading_day=1))
    db_session.add_all([MarketCalendar(trade_date=date(2026, 10, 18), is_trading_day=0),
                       MarketCalendar(trade_date=date(2026, 10, 1), is_trading_day=0)])
    watchlist = Watchlist(name="etf contract", list_type="manual")
    db_session.add(watchlist)
    db_session.flush()
    db_session.add_all([WatchlistItem(watchlist_id=watchlist.id, symbol_id=symbol.id) for symbol in symbols])
    warehouse = FactorWarehouse(tmp_path / "etf.duckdb")
    warehouse.initialize()
    with warehouse.connection() as conn:
        conn.executemany("INSERT INTO raw_asset_universe VALUES (?,?,?,?, 'cn',TRUE,'test',?,CURRENT_TIMESTAMP)",
                         [(code, code, "etf", market, i) for i, (code, market) in enumerate(
                             [("510300", "sh"), ("159915", "sz"), ("513120", "sh")], 1)])
        conn.execute("INSERT INTO raw_asset_universe VALUES ('000001','stock','stock','sz','cn',TRUE,'test',4,CURRENT_TIMESTAMP)")
        # Production mirrors stock bars only. ETF coverage must still have a denominator.
        conn.execute("""INSERT INTO raw_daily_bars
            (symbol,trade_date,adjust,close,source,source_origin,source_row_id,ingested_at,batch_id)
            VALUES ('000001','2026-10-19','qfq',10,'test','test',1,CURRENT_TIMESTAMP,'b')""")
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1, warehouse_path=str(warehouse.path), updated_by="test"))
    db_session.commit()
    app = FastAPI()
    app.include_router(external_data.router, prefix="/api/v1")
    monkeypatch.setattr(external_data, "date", Today)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client, warehouse, symbols[:3], watchlist.id


def mock_quotes(monkeypatch, *, day=TODAY, change=None):
    calls = []
    def get(url, *, params, **kwargs):
        calls.append((url, params, kwargs))
        assert url == provider.URL
        assert params["fields"] == provider.FIELDS
        codes = [value.split(".")[1] for value in params["secids"].split(",")]
        rows = [{"f12": code, "f2": .99, "f441": 1., "f38": 1000., "f402": 1.,
                 "f297": int(day.strftime("%Y%m%d")),
                 "f124": datetime(day.year, day.month, day.day, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()}
                for code in codes]
        payload = {"rc": 0, "data": {"total": len(rows), "diff": rows}}
        if change:
            change(payload)
        return Mock(json=lambda: payload)
    monkeypatch.setattr(provider.requests, "get", get)
    return calls


def run_task(client, watchlist_id):
    response = client.post("/api/v1/external-data/sync-tasks", json={
        "dataset": "etf", "source": "watchlist", "watchlist_id": watchlist_id,
        "lookback_days": 1, "mode": "incremental"})
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]
    until = time.monotonic() + 30
    while time.monotonic() < until:
        task = client.get(f"/api/v1/external-data/sync-tasks/{task_id}").json()
        if task["status"] in {"done", "failed", "cancelled"}:
            return task
        time.sleep(.05)
    pytest.fail("ETF task did not terminate")


def test_public_task_is_one_targeted_request_and_real_sql_to_warehouse(environment, db_session, monkeypatch):
    client, warehouse, symbols, watchlist_id = environment
    calls = mock_quotes(monkeypatch)
    task = run_task(client, watchlist_id)
    assert task["status"] == "done" and task["ok_count"] == 3
    assert len(calls) == 1
    assert set(calls[0][1]["secids"].split(",")) == {"1.510300", "0.159915", "1.513120"}
    result = task["result"]
    assert result["records"] == 3 and result["warehouse_mirror"]["records"] == 3
    assert result["field_coverage"]["etf_fund_size"] == {"available": 3, "total": 3}
    assert result["warnings"] and "尚未接入" in result["unavailable_fields"]["etf_tracking_error"]
    records = db_session.query(EtfIndicator).all()
    assert len(records) == 3
    for row in records:
        assert row.trade_date == TODAY and row.total_shares == 1000 and row.fund_size == 1000
        assert row.premium_discount == pytest.approx(-1)
        assert row.premium_discount_score == pytest.approx(70)
        assert row.tracking_error is None
        assert json.loads(row.raw_json)["fund_size_basis"] == "same_day_iopv_times_shares"
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_etf_indicators").fetchone()[0] == 3
    stats = _query_field_stats(warehouse)
    for field in ("etf_premium_discount", "etf_fund_size"):
        assert stats[field]["nonnull_rows"] == 3 and stats[field]["expected_rows"] == 3, (field, stats[field])
        assert stats[field]["latest_daily_coverage"] == 1
    assert stats["etf_tracking_error"]["first_date"] is None
    invalidate_formula_catalog_cache()
    coverage = client.get("/api/v1/external-data/coverage").json()
    diagnostic = next(row for row in coverage["datasets"] if row["dataset"] == "etf")
    assert diagnostic["readiness"] == "limited" and diagnostic["reason"] == "partial_fields_available"


@pytest.mark.parametrize("unavailable", ["empty", "stale", "close_only"])
def test_no_current_metrics_does_not_report_success_or_mirror_local_prices(environment, db_session, monkeypatch, unavailable):
    client, _, _, watchlist_id = environment
    def change(payload):
        if unavailable == "empty":
            payload["data"] = None
        elif unavailable == "close_only":
            for row in payload["data"]["diff"]:
                row.update(f441="-", f38="-")
    calls = mock_quotes(monkeypatch, day=TODAY - timedelta(days=3) if unavailable == "stale" else TODAY, change=change)
    task = run_task(client, watchlist_id)
    assert task["status"] == "done" and task["result"]["skip_reason"] == "data_unavailable"
    assert task["result"]["records"] == 0 and task["ok_count"] == 0
    assert task["result"]["warehouse_mirror"]["status"] == "skipped"
    assert db_session.query(EtfIndicator).count() == 0
    assert len(calls) == 1


def test_etf_history_backfill_writes_only_available_fields(db_session, monkeypatch):
    symbol = Symbol(symbol="510300", name="history-etf", market="sh", asset_type="etf")
    db_session.add(symbol)
    db_session.flush()
    db_session.add(DailyBar(
        symbol_id=symbol.id, trade_date=date(2026, 1, 5),
        open=4.0, high=4.1, low=3.9, close=4.05, source="test",
    ))
    db_session.commit()
    monkeypatch.setattr(
        etf, "_fetch_etf_nav_history",
        lambda *_a, **_k: {date(2026, 1, 5): 4.0, date(2026, 1, 6): 4.02},
    )

    summary = etf.sync_etf_indicator_range(
        db_session, symbol, date(2026, 1, 5), date(2026, 1, 6)
    )

    assert summary["written"] == 2
    rows = {
        row.trade_date: row
        for row in db_session.query(EtfIndicator).filter_by(symbol_id=symbol.id)
    }
    first = rows[date(2026, 1, 5)]
    assert first.nav == 4.0
    assert first.close == 4.05
    assert first.premium_discount == pytest.approx((4.05 - 4.0) / 4.0 * 100)
    assert first.premium_discount_score == etf.calc_premium_discount_score(first.premium_discount)
    assert first.total_shares is None
    assert first.fund_size is None
    assert rows[date(2026, 1, 6)].nav == 4.02
    assert rows[date(2026, 1, 6)].total_shares is None


def test_provider_outage_has_two_attempts_and_no_history_fallback(environment, db_session, monkeypatch):
    client, _, _, watchlist_id = environment
    get = Mock(side_effect=ConnectionError("provider unavailable"))
    monkeypatch.setattr(provider.requests, "get", get)
    task = run_task(client, watchlist_id)
    assert task["status"] == "failed" and get.call_count == 2
    assert db_session.query(EtfIndicator).count() == 0


@pytest.mark.parametrize("bad", ["date", "duplicate", "outside_scope"])
def test_bad_provider_contract_fails_before_any_write(environment, db_session, monkeypatch, bad):
    client, _, _, watchlist_id = environment
    def change(payload):
        row = payload["data"]["diff"][-1]
        if bad == "date":
            row["f297"] = 20261016
        else:
            row["f12"] = payload["data"]["diff"][0]["f12"] if bad == "duplicate" else "599999"
    calls = mock_quotes(monkeypatch, change=change)
    assert run_task(client, watchlist_id)["status"] == "failed"
    assert len(calls) == 1 and db_session.query(EtfIndicator).count() == 0


def test_legacy_api_and_scoring_share_the_bounded_provider(environment, db_session, monkeypatch):
    client, _, symbols, _ = environment
    calls = mock_quotes(monkeypatch)
    response = client.post("/api/v1/external-data/etf-indicators/sync?source=watchlist")
    assert response.status_code == 200 and response.json()["records"] == 3
    assert len(calls) == 1 and response.json()["warnings"]
    item = etf.sync_etf_indicator(db_session, symbols[0])
    assert item.premium_discount == pytest.approx(-1) and len(calls) == 2
    assert db_session.query(EtfIndicator).count() == 3


def test_cancelled_batch_does_not_fetch(environment, db_session, monkeypatch):
    _, _, symbols, _ = environment
    get = Mock()
    monkeypatch.setattr(provider.requests, "get", get)
    with pytest.raises(InterruptedError):
        etf.sync_etf_snapshots(db_session, symbols, TODAY, should_cancel=lambda: True)
    get.assert_not_called()


def test_large_scope_is_batched_and_does_not_expand_request_fields(environment, db_session, monkeypatch):
    mock_calls = mock_quotes(monkeypatch)
    symbols = [Symbol(symbol=f"51{i:04d}", name="ETF", market="sh", asset_type="etf") for i in range(101)]
    db_session.add_all(symbols)
    db_session.flush()
    result = etf.sync_etf_snapshots(db_session, symbols, TODAY)
    assert result["records"] == 101 and len(mock_calls) == 2
    assert [len(call[1]["secids"].split(",")) for call in mock_calls] == [100, 1]
