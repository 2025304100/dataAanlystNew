"""Capital-flow formula previews agree with complete local observations."""
from datetime import date, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import factors
from app.db.session import get_db
from app.models.factor_runtime import FactorSystemConfig
from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox
FORMULA = "sum(main_net_inflow, 5) / sum(amount, 5)"
DAY = date(2026, 10, 16)


@pytest.fixture
def flow_preview(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "flow-preview.duckdb")
    warehouse.initialize()
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1,
        warehouse_path=str(warehouse.path), updated_by="isolated_test"))
    db_session.commit()
    app = FastAPI()
    app.include_router(factors.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client, warehouse


def seed(warehouse, *, missing=False, observations=5):
    bars, flows = [], []
    for i in range(observations):
        day = DAY-timedelta(days=observations-1-i)
        bars.append(dict(symbol="000001", trade_date=day, adjust="qfq", amount=1000,
            source="test", source_origin="test", source_row_id=i+1,
            ingested_at=datetime(2026, 10, 17), batch_id="bars"))
        if not missing or i != 2:
            flows.append(dict(symbol="000001", trade_date=day, main_net_inflow=10*(i+1),
                source="test", ingested_at=datetime(2026, 10, 17), batch_id="flows"))
    warehouse.upsert_daily_bars(bars, source_key="test.bars", watermark=observations)
    warehouse.upsert_records("raw_fund_flows", flows)


def preview(client, formula=FORMULA):
    response = client.post("/api/v1/factors/preview", json={"formula_expr": formula,
        "trade_date": str(DAY), "symbols": ["000001"], "max_symbols": 1})
    assert response.status_code == 200, response.text
    return response.json()


def test_five_day_inflow_preview_does_not_sum_a_window_with_a_missing_flow(flow_preview):
    client, warehouse = flow_preview
    seed(warehouse, missing=True)
    body = preview(client)
    assert body["is_valid"] is True
    assert len(body["values"]) == 1
    assert body["values"][0]["raw_value"] is None
    assert body["values"][0]["eligible"] is False


def test_fund_flow_preview_uses_the_latest_source_once_per_stock_day(flow_preview):
    client, warehouse = flow_preview
    seed(warehouse)
    warehouse.upsert_records("raw_fund_flows", [dict(symbol="000001", trade_date=DAY-timedelta(days=2),
        main_net_inflow=60, source="independent", ingested_at=datetime(2026, 10, 18), batch_id="new-receipt")])
    body = preview(client)
    # Current daily inputs are 10,20,60,40,50; five amounts are 1,000 each.
    assert body["values"][0]["raw_value"] == pytest.approx(0.036)
    assert body["values"][0]["eligible"] is True
    plan = compile_formula(formula=FORMULA).execution_plan
    panel = FactorExecutor(warehouse).execute_panel(plan, start_date=DAY, end_date=DAY, symbols=["000001"])
    assert panel.read_errors == []
    assert panel.factors_long.iloc[0]["raw_value"] == pytest.approx(0.036)


def test_warm_preview_and_catalog_reread_a_committed_historical_correction(flow_preview):
    client, warehouse = flow_preview
    seed(warehouse, missing=True)
    def catalog_flow():
        response = client.get("/api/v1/factors/formula-catalog")
        assert response.status_code == 200
        return next(field for field in response.json()["fields"] if field["key"] == "main_net_inflow")
    assert catalog_flow()["nonnull_rows"] == 4
    assert preview(client)["values"][0]["eligible"] is False
    # Same code/date/formula immediately after a real DuckDB commit. No sleep,
    # explicit cache clear or model recalculation is needed for a fresh read.
    warehouse.upsert_records("raw_fund_flows", [dict(symbol="000001", trade_date=DAY-timedelta(days=2),
        main_net_inflow=30, source="test", ingested_at=datetime(2026, 10, 18), batch_id="fill")])
    assert catalog_flow()["nonnull_rows"] == 5
    repaired = preview(client)
    assert repaired["values"][0]["raw_value"] == pytest.approx(0.03)
    assert repaired["values"][0]["eligible"] is True
    assert preview(client)["values"] == repaired["values"]


def test_four_observations_cannot_be_presented_as_the_five_day_factor(flow_preview):
    client, warehouse = flow_preview
    seed(warehouse, observations=4)
    assert preview(client)["values"][0]["raw_value"] is None
    # The fix is confined to this registered complete-window factor.
    assert preview(client, "sum(main_net_inflow, 5)")["values"][0]["raw_value"] == 100


def test_zero_turnover_is_missing_rather_than_an_eligible_infinite_ratio(flow_preview):
    client, warehouse = flow_preview
    seed(warehouse)
    with warehouse.connection() as connection:
        connection.execute("UPDATE raw_daily_bars SET amount=0")
    value = preview(client)["values"][0]
    assert value["raw_value"] is None and value["eligible"] is False
