"""Public preview must warm rolling factors with real trading dates."""
from datetime import date

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import factors
from app.db.session import get_db
from app.models.factor_runtime import FactorSystemConfig
from app.services.factors.store import FactorWarehouse


GUARDED_TURNOVER = (
    "iif(count(turnover_rate-turnover_rate,20)==20,"
    "iif(stddev(turnover_rate,20)>1e-8,"
    "(turnover_rate-mean(turnover_rate,20))/stddev(turnover_rate,20),0),None)"
)
TARGET_DATE = date(2026, 10, 14)


@pytest.fixture
def preview_client(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    path = tmp_path / "preview.duckdb"
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(path))
    warehouse = FactorWarehouse(path)
    warehouse.initialize()
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1,
        warehouse_path=str(path), updated_by="isolated_test"))
    db_session.commit()
    app = FastAPI()
    app.include_router(factors.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client, warehouse


def _dates():
    # The 2026 National Day closure leaves fewer than 20 observations in the
    # final 30 calendar days even though older real bars are available.
    return [stamp.date() for stamp in pd.bdate_range("2026-08-17", TARGET_DATE)
        if stamp.date() != date(2026, 9, 25)
        and not date(2026, 10, 1) <= stamp.date() <= date(2026, 10, 8)]


def _seed(warehouse, *, missing=False, only_nineteen=False, future=False):
    dates = _dates()[-19:] if only_nineteen else _dates()
    values = [float(index + 1) for index in range(len(dates))]
    rows = []
    for symbol in ("000001", "000002"):
        for index, trade_date in enumerate(dates):
            value = None if missing and symbol == "000001" and index == len(dates) - 10 else values[index]
            rows.append((symbol, trade_date, value))
    if future:
        rows.append(("000001", date(2026, 10, 15), 999_999.0))
    with warehouse.safe_write_context() as connection:
        connection.executemany("""INSERT INTO raw_daily_bars
            (symbol,trade_date,adjust,close,turnover_rate,source,source_origin,source_row_id,ingested_at,batch_id)
            VALUES (?,?,'qfq',17.,?,'isolated','isolated',1,now(),'isolated')""", rows)
        for symbol in ("000001", "000002"):
            connection.execute("""INSERT INTO raw_asset_universe
                (symbol,name,asset_type,market,region,is_active,source,source_row_id,updated_at)
                VALUES (?,?,'stock','sz','cn',true,'isolated',1,now())""", [symbol, symbol])
    return values


def _preview(client):
    response = client.post("/api/v1/factors/preview", json={
        "formula_expr": GUARDED_TURNOVER, "trade_date": TARGET_DATE.isoformat(),
        "symbols": ["000001"], "max_symbols": 1,
        "postprocess": {"winsorize": {"method": "mad", "mad_multiplier": 3.0},
            "zscore": {"ddof": 0}, "missing_policy": "exclude"},
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("future", [False, True])
def test_holiday_preview_reads_twenty_trading_observations_before_limiting_output(preview_client, future):
    client, warehouse = preview_client
    values = _seed(warehouse, future=future)
    body = _preview(client)
    assert body["is_valid"] is True
    assert body["attempted_count"] == 1
    assert body["valid_count"] == 1, body["missing_reasons"]
    assert body["evaluation_supported"] is True
    assert len(body["values"]) == 1
    value = body["values"][0]
    assert value["symbol"] == "000001"
    assert value["eligible"] is True
    expected = (values[-1] - np.mean(values[-20:])) / np.std(values[-20:], ddof=0)
    assert value["raw_value"] == pytest.approx(expected)


def test_missing_value_inside_last_twenty_rows_is_not_backfilled_from_older_values(preview_client):
    client, warehouse = preview_client
    _seed(warehouse, missing=True)
    body = _preview(client)
    assert body["is_valid"] is True
    assert body["valid_count"] == 0
    assert body["evaluation_supported"] is False
    assert body["values"][0]["raw_value"] is None
    assert body["values"][0]["eligible"] is False
    assert body["missing_reasons"]["evaluation"] == "no_valid_preview_values"


def test_nineteen_real_observations_do_not_become_a_complete_twenty_row_window(preview_client):
    client, warehouse = preview_client
    _seed(warehouse, only_nineteen=True)
    body = _preview(client)
    assert body["valid_count"] == 0
    assert body["evaluation_supported"] is False
    assert body["values"][0]["raw_value"] is None
