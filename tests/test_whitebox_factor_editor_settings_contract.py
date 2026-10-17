"""Public factor-setting contracts on an isolated in-memory database."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import factor_sets, factors
from app.db.base import Base
from app.db.session import get_db
from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorVersion
from app.models.factor_runtime import FactorSystemConfig

pytestmark = pytest.mark.whitebox


@pytest.fixture
def settings_client(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Factor.__table__, FactorVersion.__table__,
                                           FactorSet.__table__, FactorSetMember.__table__, FactorSystemConfig.__table__])
    sessions = sessionmaker(bind=engine)
    postprocess = {"winsorize": {"method": "mad", "mad_multiplier": 4.5},
                   "rank": {"ascending": False}, "missing_policy": "impute_zero"}
    with sessions() as db:
        db.add_all([
            FactorSystemConfig(id=1, feature_enabled=1, warehouse_path=":unused:"),
            Factor(id=1, code="settings_a", name="A", category="technical", direction="higher_better", status="active"),
            Factor(id=2, code="settings_b", name="B", category="technical", direction="higher_better", status="active"),
        ])
        db.flush()
        db.add_all([
            FactorVersion(id=101, factor_id=1, version=1, formula_expr="close", postprocess_json=json.dumps(postprocess), validation_status="valid"),
            FactorVersion(id=102, factor_id=2, version=1, formula_expr="volume", validation_status="valid"),
            FactorSet(id="settings-draft", name="Settings draft", status="draft"),
        ])
        db.flush()
        db.add_all([
            FactorSetMember(id=2, factor_set_id="settings-draft", factor_id=1, factor_version_id=101, factor_code="settings_a", factor_version=1),
            FactorSetMember(id=11, factor_set_id="settings-draft", factor_id=2, factor_version_id=102, factor_code="settings_b", factor_version=1),
        ])
        db.commit()
    app = FastAPI()
    app.include_router(factors.router, prefix="/api/v1")
    app.include_router(factor_sets.router, prefix="/api/v1")

    def isolated_session():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_session
    monkeypatch.setattr(factor_sets, "SessionLocal", sessions)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        engine.dispose()


def test_version_list_preserves_nondefault_postprocess_for_reopening_editor(settings_client):
    response = settings_client.get("/api/v1/factors/settings_a/versions")
    assert response.status_code == 200, response.text
    assert response.json()[0]["postprocess"] == {
        "winsorize": {"method": "mad", "mad_multiplier": 4.5},
        "rank": {"ascending": False}, "missing_policy": "impute_zero",
    }


def test_member_delete_contract_targets_factor_id_despite_a_colliding_member_id(settings_client):
    before = settings_client.get("/api/v1/factor-sets/settings-draft").json()["members"]
    assert [(member["id"], member["factor_id"]) for member in before] == [(2, 1), (11, 2)]
    removed = settings_client.delete("/api/v1/factor-sets/settings-draft/members/1")
    assert removed.status_code == 200, removed.text
    assert removed.json()["factor_id"] == 1
    remaining = settings_client.get("/api/v1/factor-sets/settings-draft").json()["members"]
    assert [(member["id"], member["factor_code"]) for member in remaining] == [(11, "settings_b")]


def test_reopened_version_postprocess_survives_creation_and_readback(settings_client):
    original = settings_client.get("/api/v1/factors/settings_a/versions").json()[0]
    created = settings_client.post("/api/v1/factors/settings_a/versions", json={
        "formula_expr": "close + 1", "params": {}, "direction": original["direction"],
        "postprocess": original["postprocess"], "created_via": "manual",
    })
    assert created.status_code == 201, created.text
    latest = settings_client.get("/api/v1/factors/settings_a/versions").json()[0]
    assert latest["version"] == 2
    assert latest["formula_expr"] == "close + 1"
    assert latest["postprocess"] == original["postprocess"]
