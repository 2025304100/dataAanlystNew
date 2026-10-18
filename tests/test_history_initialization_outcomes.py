"""Public initialization worker/status contracts, isolated SQLite + supplier SDK."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from threading import Event

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import market_data as routes
from app.models.symbol import Symbol
from app.services import akshare_registry, market_data
from app.services.async_tasks import get_async_task

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("outcomes,expected", [([False], "failed"), ([True, False], "partial"), ([True], "completed")])
def test_initialization_reports_real_supplier_outcome(db_session, monkeypatch, outcomes, expected):
    # Use only the temporary database configured by db_session; no live task
    # snapshot, thread, or network transport can escape this test.
    monkeypatch.setattr(market_data, "_HISTORY_INIT_TASK", deepcopy(market_data._HISTORY_INIT_TASK))
    monkeypatch.setattr(market_data, "_HISTORY_INIT_CANCEL_EVENT", Event())
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {key: {"enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1} for key in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx")})
    symbols = []
    for index in range(len(outcomes)):
        symbol = Symbol(symbol=f"60000{index}", name="测试标的", asset_type="stock", market="sh", is_active=1)
        db_session.add(symbol)
        symbols.append(symbol)
    db_session.commit()
    successful = {symbol.symbol for symbol, ok in zip(symbols, outcomes) if ok}

    def supplier(*args, **kwargs):
        code = str(kwargs.get("symbol", ""))[-6:]
        if code not in successful:
            raise ValueError("isolated supplier failure")
        return pd.DataFrame([{"日期": "2026-10-01", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10, "成交量": 100, "成交额": 1000, "换手率": 1}])

    for name in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx"):
        monkeypatch.setattr(market_data.ak, name, supplier)
    monkeypatch.setattr(market_data, "fetch_sina_history_raw", supplier, raising=False)
    def failed_tx_http(*_args, **_kwargs):
        raise ValueError("isolated Tencent HTTP supplier failure")
    monkeypatch.setattr("app.services.market_data_sources.sources.tencent_history.requests.get", failed_tx_http)
    snapshot = market_data.start_history_initialization_task(symbol_ids=[symbol.id for symbol in symbols], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1), repair_mode="bars")
    market_data.run_history_initialization_task(snapshot["task_id"])
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    response = TestClient(app).get("/api/v1/market-data/initialize-history/status")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == expected
    assert result["summary"]["sync_ok_count"] == sum(outcomes)
    assert result["summary"]["sync_failed_count"] == len(outcomes) - sum(outcomes)
    task = get_async_task(snapshot["task_id"])
    assert task.status == {"completed": "done", "partial": "partial", "failed": "failed"}[expected]
    assert task.is_terminal_locked


def test_failed_score_commit_is_not_counted_and_does_not_erase_prior_days(db_session, monkeypatch):
    from sqlalchemy import event
    from app.api.routes import scores
    from app.db.session import get_db
    from app.models.daily_bar import DailyBar

    monkeypatch.setattr(market_data, "_HISTORY_INIT_TASK", deepcopy(market_data._HISTORY_INIT_TASK))
    monkeypatch.setattr(market_data, "_HISTORY_INIT_CANCEL_EVENT", Event())
    symbol = Symbol(symbol="600010", name="提交测试", asset_type="stock", market="sh", is_active=1)
    db_session.add(symbol)
    db_session.flush()
    for day in (1, 2, 3):
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026, 10, day), open=10, high=11, low=9, close=10, volume=100))
    db_session.commit()
    engine = db_session.get_bind()
    score_commits = []

    def mark_score_write(connection, cursor, statement, parameters, context, many):
        if statement.lower().lstrip().startswith("insert into scores"):
            connection.info["test_score_write"] = True

    def reject_second_score_commit(connection):
        if connection.info.pop("test_score_write", False):
            score_commits.append(True)
            if len(score_commits) == 2:
                raise RuntimeError("isolated score commit rejected")

    event.listen(engine, "before_cursor_execute", mark_score_write)
    event.listen(engine, "commit", reject_second_score_commit)
    try:
        snapshot = market_data.start_history_initialization_task(symbol_ids=[symbol.id], start_date=date(2026, 10, 1), end_date=date(2026, 10, 3), repair_mode="scores")
        market_data.run_history_initialization_task(snapshot["task_id"])
    finally:
        event.remove(engine, "before_cursor_execute", mark_score_write)
        event.remove(engine, "commit", reject_second_score_commit)
    result = market_data.get_history_initialization_status()
    assert result["status"] == "partial"
    assert result["summary"]["score_days_completed"] == 2
    task = get_async_task(snapshot["task_id"])
    assert task.ok_count == 2
    assert task.failed_count == 1
    app = FastAPI()
    app.include_router(scores.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    response = TestClient(app).get(f"/api/v1/scores/history/{symbol.id}")
    assert response.status_code == 200, response.text
    assert {row["trade_date"] for row in response.json()} == {"2026-10-01", "2026-10-03"}
