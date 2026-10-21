"""Observable completion contracts of the real market-data task dispatcher."""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from threading import Event

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import event

from app.api.routes.market_data import get_daily_bars
from app.api.routes.scores import get_latest_score
from app.core.config import settings
from app.db.session import get_session_local
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.schemas.async_task import MarketDataSyncCreate
from app.services.async_tasks import get_async_task, request_all_workers_stop, wait_workers_stopped
from app.services.market_data_sync_task import create_market_data_sync_task


pytestmark = [pytest.mark.whitebox, pytest.mark.reap_workers]
DATA_DAY = date(2026, 10, 19)


def _completed(task_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        task = get_async_task(task_id)
        if task and task.status in {"done", "partial", "failed", "cancelled"}:
            return task
        time.sleep(.02)
    raise AssertionError(f"Market task {task_id} did not complete")


@pytest.fixture
def supplied_market(db_session, tmp_path, monkeypatch):
    symbol = Symbol(symbol="510300", name="Completion ETF", asset_type="etf", market="sh", is_active=1)
    portfolio = Portfolio(name="completion-main", is_default=1, account_type="simulated",
                          asset_scope="stock_etf", total_capital=100000,
                          investable_ratio=.9, cash_reserve_ratio=.1)
    db_session.add_all([symbol, portfolio])
    db_session.commit()
    symbol_id = symbol.id
    monkeypatch.setattr(settings, "factor_warehouse_path", tmp_path / "completion-factors.duckdb")
    requested, release = Event(), Event()

    def response(self, method, url, **kwargs):
        if "push2his.eastmoney.com/api/qt/stock/kline/get" not in str(url):
            raise requests.ConnectionError("Bounded supplier: other upstreams unavailable")
        requested.set()
        if not release.wait(5):
            raise requests.Timeout("Supplier bytes were not released")
        klines = []
        for offset in range(119, -1, -1):
            day = DATA_DAY - timedelta(days=offset)
            if day.weekday() < 5:
                klines.append(f"{day.isoformat()},10,10,10.2,9.8,10000,10000000,4,0,0,1")
        result = requests.Response()
        result.status_code = 200
        result.url = str(url)
        result._content = json.dumps({"data": {"code": "510300", "klines": klines}}).encode()
        return result

    monkeypatch.setattr(requests.Session, "request", response)
    payload = MarketDataSyncCreate(scope="symbols", symbol_ids=[symbol_id], asset_types=["etf"],
                                   end_date=DATA_DAY, auto_scan=False)
    try:
        yield payload, requested, release, symbol_id
    finally:
        release.set()
        request_all_workers_stop()
        wait_workers_stopped(5)


def test_score_rolled_back_by_plan_failure_is_not_reported_as_completed(supplied_market, db_session):
    payload, _, release, symbol_id = supplied_market
    release.set()
    rejected_plan = Event()
    engine = db_session.get_bind()

    def reject_plan_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO TRADE_SETUPS"):
            rejected_plan.set()
            raise RuntimeError("Isolated database boundary: trade plan write failed")

    event.listen(engine, "before_cursor_execute", reject_plan_write)
    try:
        started = create_market_data_sync_task(payload)
        completed = _completed(started["id"])
        assert rejected_plan.is_set(), completed.errors
        assert completed.status == "done"
        assert completed.result["ok_count"] == 1
        assert completed.result["scored_count"] == 0
        with get_session_local()() as reader:
            assert get_daily_bars(symbol_id, limit=120, db=reader)
            with pytest.raises(HTTPException) as missing:
                get_latest_score(symbol_id, db=reader)
        assert missing.value.status_code == 404
    finally:
        event.remove(engine, "before_cursor_execute", reject_plan_write)


def test_completed_market_request_runs_again_while_active_request_is_shared(supplied_market):
    payload, requested, release, symbol_id = supplied_market
    first = create_market_data_sync_task(payload)
    try:
        assert requested.wait(5)
        duplicate = create_market_data_sync_task(payload)
        assert duplicate["id"] == first["id"]
    finally:
        release.set()
    finished = _completed(first["id"])
    assert finished.status == "done", finished.errors
    assert finished.result["scored_count"] == 1

    repeated = create_market_data_sync_task(payload)
    assert repeated["id"] != first["id"]
    completed_again = _completed(repeated["id"])
    assert completed_again.status == "done", completed_again.errors
    assert completed_again.result["scored_count"] == 1
    with get_session_local()() as reader:
        assert get_latest_score(symbol_id, db=reader).trade_date == DATA_DAY
