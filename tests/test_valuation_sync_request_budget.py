"""Public incremental valuation tasks must not amplify market-wide requests."""
from collections import Counter
from datetime import date, timedelta
import time

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import external_data
from app.db.session import get_db
from app.models.factor_runtime import FactorSystemConfig
from app.models.symbol import Symbol
from app.services import fundamental_data
from app.services.factors.store import FactorWarehouse


@pytest.fixture
def valuation_client(db_session, tmp_path, monkeypatch):
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
    app = FastAPI()
    app.include_router(external_data.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def _run(client):
    created = client.post("/api/v1/external-data/sync-tasks", json={
        "dataset": "fundamental", "source": "all", "mode": "incremental",
        "lookback_days": 1, "include_northbound": False, "max_workers": 1,
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


def test_failed_batch_snapshot_is_not_repeated_for_each_stock(valuation_client, monkeypatch):
    calls = Counter()

    def market():
        calls["market"] += 1
        raise ConnectionError("isolated unavailable snapshot")

    def history(symbol):
        calls["history"] += 1
        return pd.DataFrame([{"数据日期": date.today(), "PE(TTM)": 12.,
            "市净率": 1.2, "总市值": 100., "流通市值": 80.}])

    def info(symbol):
        calls["industry"] += 1
        raise ConnectionError("unnecessary industry request")

    monkeypatch.setattr(fundamental_data.ak, "stock_zh_a_spot_em", market)
    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", history)
    monkeypatch.setattr(fundamental_data.ak, "stock_individual_info_em", info)
    task = _run(valuation_client)
    assert task["status"] == "done", task
    assert task["ok_count"] == 2
    # One market-wide request budget with the configured three attempts;
    # each stock then uses exactly one independent valuation-history call.
    assert calls["market"] == 3
    assert calls["history"] == 2
    assert calls["industry"] == 0
    assert task["result"]["field_coverage"]["industry"]["missing"] == 1


def test_missing_requested_day_is_failed_instead_of_writing_old_valuation(valuation_client, monkeypatch):
    def market():
        raise ConnectionError("isolated unavailable snapshot")

    def history(symbol):
        return pd.DataFrame([{"数据日期": date.today() - timedelta(days=1),
            "PE(TTM)": 12., "市净率": 1.2, "总市值": 100., "流通市值": 80.}])

    monkeypatch.setattr(fundamental_data.ak, "stock_zh_a_spot_em", market)
    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", history)
    task = _run(valuation_client)
    assert task["status"] == "failed", task
    assert task["ok_count"] == 0
    assert task["failed_count"] == 2
    assert task["result"]["records"] == 0
    assert any("所选日期范围" in error for error in task["result"]["errors"])


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

    monkeypatch.setattr(fundamental_data.ak, "stock_zh_a_spot_em", market)
    monkeypatch.setattr(fundamental_data.ak, "stock_value_em", forbidden)
    monkeypatch.setattr(fundamental_data.ak, "stock_individual_info_em", forbidden)
    task = _run(valuation_client)
    assert task["status"] == "done", task
    assert task["ok_count"] == 2
    assert calls == {"market": 1}
