"""Public preflight diagnostics use canonical data fields, even for legacy rows."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import factor_evaluation
from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from app.models.factor_runtime import FactorSystemConfig
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


@pytest.fixture
def legacy_preflight(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    warehouse_path = tmp_path / "preflight.duckdb"
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(warehouse_path))
    warehouse = FactorWarehouse(warehouse_path)
    warehouse.initialize()
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1,
        warehouse_path=str(warehouse_path), updated_by="isolated_test"))
    db_session.commit()
    app = FastAPI()
    app.include_router(factor_evaluation.router, prefix="/api/v1")
    with TestClient(app) as client:
        yield client, warehouse


def _legacy_version(db_session, formula):
    factor = Factor(code="legacy_preflight", name="Legacy preflight", category="technical",
        direction="higher_better", status="active", is_active=1, origin="imported",
        lifecycle_status="testing", factor_kind="continuous")
    db_session.add(factor)
    db_session.flush()
    version = FactorVersion(factor_id=factor.id, version=1, formula_expr=formula,
        direction="higher_better", params_json="{}", source_mapping_json="{}",
        validation_status="pending", is_latest=1)
    db_session.add(version)
    db_session.commit()
    return version


def _preflight(client, version):
    response = client.post("/api/v1/factor-evaluation/preflight", json={
        "factor_code": "legacy_preflight", "factor_version_id": str(version.id),
    })
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    formula = next(item for item in items if item["code"].startswith("preflight.formula_compile"))
    dependencies = next(item for item in items if item["code"].startswith("preflight.data_dependencies"))
    return response.json(), formula, dependencies


@pytest.mark.parametrize("expression", [
    "(turnover - mean_20d) / stddev_pop_20d",
    "mean(turnover, 20) + missing_old_field",
])
def test_invalid_legacy_formula_keeps_compile_blocker_without_fake_sync_fields(
    legacy_preflight, db_session, expression,
):
    client, _warehouse = legacy_preflight
    version = _legacy_version(db_session, expression)
    body, formula, dependencies = _preflight(client, version)
    assert formula["code"] == "preflight.formula_compile.error"
    assert formula["severity"] == "error"
    assert formula["fix_link"]["tab"] == "factors"
    assert dependencies["evidence"]["required_fields"] == ["turnover_rate"]
    assert dependencies["evidence"]["missing"] == []
    assert dependencies["code"] == "preflight.data_dependencies.ok"
    assert not any(item["code"] == "preflight.factor_values.ok" for item in body["items"])
    db_session.refresh(version)
    assert version.formula_expr == expression
    assert version.validation_status == "pending"


@pytest.mark.parametrize("expression,expected", [("turnover", "turnover_rate"), ("close", "close")])
def test_valid_legacy_bare_field_uses_compiler_canonical_dependencies(
    legacy_preflight, db_session, expression, expected,
):
    client, _warehouse = legacy_preflight
    version = _legacy_version(db_session, expression)
    _body, formula, dependencies = _preflight(client, version)
    assert formula["code"] == "preflight.formula_compile.ok"
    assert dependencies["evidence"]["required_fields"] == [expected]
    assert dependencies["evidence"]["missing"] == []


def test_valid_bare_field_still_reports_actual_missing_warehouse_column(legacy_preflight, db_session):
    client, warehouse = legacy_preflight
    version = _legacy_version(db_session, "close")
    with warehouse.safe_write_context() as connection:
        connection.execute("ALTER TABLE raw_daily_bars DROP COLUMN close")
    _body, formula, dependencies = _preflight(client, version)
    assert formula["code"] == "preflight.formula_compile.ok"
    assert dependencies["code"] == "preflight.data_dependencies.missing"
    assert dependencies["evidence"]["required_fields"] == ["close"]
    assert dependencies["evidence"]["missing"] == ["close"]
