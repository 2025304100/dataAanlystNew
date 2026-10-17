"""Existing stored validation never substitutes for current executable capability."""
from __future__ import annotations

from datetime import datetime
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def compatibility_client(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(tmp_path / "warehouse.duckdb"))
    from app.api.routes import factors, factor_sets, scoring_facade
    from app.db.session import get_db
    app = FastAPI()
    for router in (factors.router, factor_sets.router, scoring_facade.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def _legacy_set(db, *, formula, code="turnover_z20", frozen=True,
                params=None, source=None, postprocess=None, direction="higher_better", status="valid"):
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion
    from app.models.factor_evaluation import FactorSet, FactorSetMember
    factor = Factor(code=code, name=code, category="technical", source_type="manual",
                    frequency="daily", direction=direction, status="active",
                    lifecycle_status="active", is_active=1)
    db.add(factor)
    db.flush()
    version = FactorVersion(factor_id=factor.id, version=2, formula_expr=formula,
        params_json=json.dumps(params or {}), source_mapping_json=json.dumps(source or {}),
        direction=direction, postprocess_json=json.dumps(postprocess),
        compiler_version="wp2-1.0.0", execution_plan_hash="old-compiled-hash",
        validation_status=status, is_latest=1)
    db.add(version)
    db.flush()
    fs = FactorSet(id="legacy-compatibility", name="Existing immutable set",
        status="frozen" if frozen else "draft", content_hash="c" * 64 if frozen else None,
        frozen_at=datetime(2026, 1, 1) if frozen else None)
    db.add(fs)
    db.flush()
    db.add(FactorSetMember(factor_set_id=fs.id, factor_id=factor.id,
        factor_version_id=version.id, factor_code=code, factor_version=2,
        role="feature", missing_policy="exclude"))
    db.commit()
    return fs.id, version.id


def test_stored_valid_legacy_turnover_cannot_claim_current_training_readiness(compatibility_client, db_session):
    set_id, version_id = _legacy_set(db_session,
        formula="(turnover - mean_20d) / stddev_pop_20d",
        params={"minimum_periods": 20, "window": 20},
        source={"field": "turnover_rate", "table": "raw_daily_bars"}, direction="nonlinear")
    readiness = compatibility_client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness")
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["ready"] is False
    assert readiness.json()["code"] == "MEMBER_VERSION_NOT_TRAINABLE"
    summary = compatibility_client.get("/api/v1/scoring/factorsets").json()[0]
    assert summary["members"][0]["version_trainable"] is False
    historical = compatibility_client.get("/api/v1/factors/turnover_z20/versions").json()[0]
    assert (historical["id"], historical["validation_status"], historical["compiler_version"]) == (
        version_id, "valid", "wp2-1.0.0")
    definition = compatibility_client.get("/api/v1/scoring/factor-definitions/turnover_z20").json()
    assert definition["versions"][0]["trainable"] is False


@pytest.mark.parametrize("code", ["ep_ttm", "negative_pb", "roe_yoy_growth",
    "main_inflow_5d_ratio", "lhb_institution_net_ratio", "turnover_z20",
    "hot_rank_attention", "tail_accumulation_proxy"])
def test_exact_native_definition_keeps_its_real_execution_capability(compatibility_client, db_session, code):
    # Seed the actual native metadata, including legacy conditional notation.
    from app.services.factors.definitions import FACTOR_BY_CODE, _PARAMS, _SOURCE_MAPPINGS
    definition = FACTOR_BY_CODE[code]
    set_id, _ = _legacy_set(db_session, code=code, formula=definition.formula,
        params=_PARAMS[code], source=_SOURCE_MAPPINGS[code], direction=definition.direction)
    response = compatibility_client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness")
    assert response.status_code == 200, response.text
    assert response.json()["ready"] is True
    summary = compatibility_client.get("/api/v1/scoring/factorsets").json()[0]
    assert summary["members"][0]["version_trainable"] is True


@pytest.mark.parametrize("entrypoint", ["factor-sets", "scoring/factorsets"])
def test_freeze_rejects_stored_valid_but_currently_invalid_formula(compatibility_client, db_session, entrypoint):
    set_id, version_id = _legacy_set(db_session, frozen=False,
        formula="(turnover - mean_20d) / stddev_pop_20d")
    response = compatibility_client.post(f"/api/v1/{entrypoint}/{set_id}/freeze", json={"reason": "Compatibility check"})
    assert response.status_code in (400, 422), response.text
    detail = compatibility_client.get(f"/api/v1/factor-sets/{set_id}").json()
    assert detail["status"] == "draft"
    assert detail["members"][0]["factor_version_id"] == version_id
    assert detail["members"][0]["version_trainable"] is False


@pytest.mark.parametrize("formula", ["close", "turnover", "turnover / mean(turnover, 20)"])
@pytest.mark.parametrize("entrypoint", ["factor-sets", "scoring/factorsets"])
def test_legal_old_compiler_formula_can_still_freeze(compatibility_client, db_session, entrypoint, formula):
    set_id, _ = _legacy_set(db_session, code="compatible_legacy", formula=formula, frozen=False)
    response = compatibility_client.post(f"/api/v1/{entrypoint}/{set_id}/freeze", json={"reason": "Old label still compatible"})
    assert response.status_code == 200, response.text
    readiness = compatibility_client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness").json()
    assert readiness["ready"] is True


def test_current_compilation_cannot_replace_missing_historical_validation(compatibility_client, db_session):
    set_id, _ = _legacy_set(db_session, formula="close", status=None)
    readiness = compatibility_client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness").json()
    assert readiness["ready"] is False
    assert readiness["code"] == "MEMBER_VERSION_NOT_TRAINABLE"


@pytest.mark.parametrize("change", ["formula", "params", "source", "direction"])
def test_native_factor_name_does_not_exempt_a_changed_contract(compatibility_client, db_session, change):
    from app.services.factors.definitions import FACTOR_BY_CODE, _PARAMS, _SOURCE_MAPPINGS
    code = "tail_accumulation_proxy"
    definition = FACTOR_BY_CODE[code]
    metadata = {"formula": definition.formula, "params": dict(_PARAMS[code]),
                "source": dict(_SOURCE_MAPPINGS[code]), "direction": definition.direction}
    if change == "formula":
        metadata["formula"] += " + absent_field"
    elif change == "params":
        metadata["params"]["minimum_day_minutes"] = 1
    elif change == "source":
        metadata["source"]["table"] = "raw_daily_bars"
    else:
        metadata["direction"] = "lower_better"
    set_id, _ = _legacy_set(db_session, code=code, **metadata)
    readiness = compatibility_client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness").json()
    assert readiness["ready"] is False


@pytest.mark.parametrize("metadata", [
    {"postprocess": {"neutralize": {"by": "industry"}}},
    {"postprocess": {"rank": {"method": "percentile"}, "zscore": {"ddof": 0}}},
    {"postprocess": {"rank": {"method": "ordinal"}}},
    {"source": {"field": "pe_ttm", "table": "raw_valuation_snapshots"}},
    {"source": {"fields": ["close"], "tables": ["raw_sentiment"]}},
])
def test_current_dsl_alone_cannot_authorize_unsupported_execution_contract(compatibility_client, db_session, metadata):
    set_id, _ = _legacy_set(db_session, formula="close", code="unsupported_execution", **metadata)
    readiness = compatibility_client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness").json()
    assert readiness["ready"] is False
    summary = compatibility_client.get("/api/v1/scoring/factorsets").json()[0]
    assert summary["members"][0]["version_trainable"] is False
