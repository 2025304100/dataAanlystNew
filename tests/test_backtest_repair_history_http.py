"""Backtest repair uses the public HTTP entry and an isolated supplier/database."""
from __future__ import annotations

import time

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import market_data as routes
from app.models.symbol import Symbol

pytestmark = pytest.mark.whitebox


def test_history_repair_http_keeps_the_requested_dates_and_symbol(db_session, monkeypatch):
    # Supplier SDK calls are the only behavior replaced. The actual router,
    # task creation, worker, storage and status serialization all run.
    import akshare as ak

    symbol = Symbol(symbol="600010", name="隔离测试标的", asset_type="stock", market="sh", is_active=1)
    db_session.add(symbol)
    db_session.commit()

    def supplier(*_args, **_kwargs):
        return pd.DataFrame([{"日期": "2025-04-01", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10, "成交量": 100, "成交额": 1000, "换手率": 1}])

    monkeypatch.setattr(ak, "stock_zh_a_hist", supplier)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.post("/api/v1/market-data/initialize-history", json={
            "preset": "1m", "start_date": "2025-04-01", "end_date": "2025-04-03",
            "symbol_ids": [symbol.id], "repair_mode": "bars", "auto_scan": False,
        })
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.get("/api/v1/market-data/initialize-history/status").json()
            if status["status"] != "running":
                break
            time.sleep(0.05)
        assert status["status"] != "running", "Isolated worker must finish before fixture teardown"
        submitted = response.json()
        assert submitted["start_date"] == "2025-04-01"
        assert submitted["end_date"] == "2025-04-03"
        assert status["start_date"] == "2025-04-01"
        assert status["end_date"] == "2025-04-03"
        assert status["symbol_ids"] == [symbol.id]
        assert status["repair_mode"] == "bars"
        scan_stage = next(stage for stage in status["stages"] if stage["key"] == "scan")
        assert scan_stage["total"] == 0


def test_precheck_returns_only_the_checked_symbols_for_repair_navigation(db_session):
    from sqlalchemy import text
    from app.api.routes import backtest
    from app.db.session import get_db

    first = Symbol(symbol="510300", name="隔离测试ETF", asset_type="etf", market="sh", is_active=1)
    other = Symbol(symbol="159915", name="其他组合ETF", asset_type="etf", market="sz", is_active=1)
    db_session.add_all([first, other])
    db_session.execute(text("CREATE TABLE IF NOT EXISTS trade_calendar (date DATE NOT NULL, is_trading_day INTEGER NOT NULL)"))
    db_session.execute(text("INSERT INTO trade_calendar (date, is_trading_day) VALUES ('2025-04-01', 1)"))
    db_session.commit()
    app = FastAPI()
    app.include_router(backtest.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest/precheck", json={
            "portfolio_id": 1, "symbol_ids": [first.id], "start_date": "2025-04-01", "end_date": "2025-04-01", "minimum_trade_days": 1,
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["symbols"] == [{"id": first.id, "symbol": "510300", "name": "隔离测试ETF"}]
        assert "STATUS_DATA_UNAVAILABLE" in {reason["code"] for reason in result["blocking_reasons"]}


@pytest.mark.parametrize("dates", [
    {"start_date": "2025-04-01"},
    {"end_date": "2025-04-03"},
    {"start_date": "2025-04-03", "end_date": "2025-04-01"},
    {"start_date": "2025-02-30", "end_date": "2025-04-01"},
])
def test_invalid_repair_dates_do_not_create_an_initialization_task(db_session, dates):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        before = client.get("/api/v1/market-data/initialize-history/status").json()
        response = client.post("/api/v1/market-data/initialize-history", json={"preset": "1m", **dates})
        assert response.status_code == 422, response.text
        after = client.get("/api/v1/market-data/initialize-history/status").json()
        assert after.get("task_id") == before.get("task_id")
        assert after["status"] == before["status"]
