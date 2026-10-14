from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.data_mirror import router
from app.db.session import get_db
from app.models.factor_runtime import FactorSystemConfig
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def test_status_reads_the_warehouse_already_open_by_the_running_application(db_session, tmp_path):
    path = tmp_path / "live-style-warehouse.duckdb"
    warehouse = FactorWarehouse(path)
    warehouse.upsert_daily_bars([
        {"symbol": "000001", "trade_date": date(2026, 10, 12), "adjust": "qfq",
         "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100,
         "amount": 1000, "turnover_rate": 1, "source": "isolated-test",
         "source_origin": "daily_bars", "source_row_id": 1,
         "ingested_at": datetime(2026, 10, 12), "batch_id": "test"},
    ], source_key="test", watermark=1)
    db_session.merge(FactorSystemConfig(id=1, feature_enabled=1,
        warehouse_path=str(path), updated_by="test"))
    db_session.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        response = client.get("/data-mirror/status")
    assert response.status_code == 200
    result = response.json()
    assert result["available"] is True, result
    assert result["total_rows"] == 1
    assert result["total_symbols"] == 1
    assert result["mirrored_from"] == result["mirrored_to"] == "2026-10-12"
