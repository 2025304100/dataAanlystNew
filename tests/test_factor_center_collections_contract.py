"""Factor-center collection operations use isolated public HTTP seams."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def collection_client(db_session, tmp_path, monkeypatch):
    # No application lifespan or external calls; all mutable stores are temporary.
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(tmp_path / "warehouse.duckdb"))
    from app.api.routes.factor_sets import router as sets_router
    from app.api.routes.scoring_facade import router as scoring_router
    app = FastAPI()
    app.include_router(sets_router, prefix="/api/v1")
    app.include_router(scoring_router, prefix="/api/v1")
    yield TestClient(app)


def _valid_factor(db, code="collection_factor"):
    # Database seed is a boundary fixture; results are observed only through HTTP.
    from app.models.factor import Factor
    from app.schemas.factor_library import FactorVersionCreate
    from app.services.factors.factor_registry import create_factor_version
    factor = Factor(code=code, name=code, category="trend", direction="higher_better",
                    status="active", source_type="manual", is_active=1)
    db.add(factor)
    db.flush()
    version = create_factor_version(db, factor_id=factor.id,
        request=FactorVersionCreate(formula_expr="close", created_by="contract-test"))
    db.commit()
    return factor, version


def _create_member(client, set_id, factor, version, **overrides):
    created = client.post("/api/v1/factor-sets", json={"id": set_id, "name": set_id})
    assert created.status_code == 200, created.text
    member = client.post(f"/api/v1/factor-sets/{set_id}/members", json={
        "factor_id": factor.id, "factor_version_id": version.id, **overrides,
    })
    assert member.status_code == 200, member.text


def test_copy_preserves_training_member_settings(collection_client, db_session):
    factor, version = _valid_factor(db_session)
    _create_member(collection_client, "source", factor, version,
                   role="feature", weight_constraint="negative", display_order=17,
                   missing_policy="impute_zero")

    copied = collection_client.post("/api/v1/scoring/factorsets/source/clone", json={
        "new_factorset_id": "copy", "new_name": "Editable copy", "actor": "contract-test",
    })
    assert copied.status_code == 200, copied.text
    result = collection_client.get("/api/v1/factor-sets/copy").json()
    assert result["status"] == "draft"
    member = result["members"][0]
    assert {key: member[key] for key in ("factor_code", "factor_version", "role",
            "weight_constraint", "display_order", "missing_policy")} == {
        "factor_code": "collection_factor", "factor_version": 1, "role": "feature",
        "weight_constraint": "negative", "display_order": 17, "missing_policy": "impute_zero",
    }


def test_native_freeze_rejects_a_set_without_any_training_feature(collection_client, db_session):
    factor, version = _valid_factor(db_session)
    _create_member(collection_client, "target-only", factor, version, role="target")
    response = collection_client.post("/api/v1/factor-sets/target-only/freeze", json={
        "actor": "contract-test", "reason": "Verify feature readiness",
    })
    assert response.status_code == 422, response.text
    assert collection_client.get("/api/v1/factor-sets/target-only").json()["status"] == "draft"


@pytest.mark.parametrize("entrypoint", ["factor-sets", "scoring/factorsets"])
@pytest.mark.parametrize("validation_status", ["pending", None])
def test_freeze_rechecks_pending_version_instead_of_trusting_latest_flag(
    collection_client, db_session, entrypoint, validation_status,
):
    factor, version = _valid_factor(db_session)
    _create_member(collection_client, "changed-version", factor, version)
    version.validation_status = validation_status
    db_session.commit()
    response = collection_client.post(f"/api/v1/{entrypoint}/changed-version/freeze", json={
        "actor": "contract-test", "reason": "The latest version still needs validation",
    })
    assert response.status_code in (400, 422), response.text
    assert collection_client.get("/api/v1/factor-sets/changed-version").json()["status"] == "draft"


@pytest.mark.parametrize("mode", ["ridge", "shadow"])
def test_running_model_collection_cannot_be_deprecated(collection_client, db_session, mode):
    import json
    from app.models.factor_model import FactorModelRun
    from app.models.factor_runtime import FactorRuntimeState
    factor, version = _valid_factor(db_session)
    _create_member(collection_client, "running-set", factor, version)
    frozen = collection_client.post("/api/v1/factor-sets/running-set/freeze", json={"reason": "Test"})
    assert frozen.status_code == 200
    # A running-model seed represents an existing installation, never activation.
    db_session.add(FactorModelRun(id="running-model", status="validated",
                                 hyperparameters_json=json.dumps({"factor_set_id": "running-set"})))
    db_session.flush()
    db_session.add(FactorRuntimeState(id=1, weight_mode=mode, active_model_run_id="running-model"))
    db_session.commit()
    response = collection_client.post("/api/v1/factor-sets/running-set/deprecate", json={
        "reason": "Do not invalidate an executing model",
    })
    assert response.status_code == 422, response.text
    assert collection_client.get("/api/v1/factor-sets/running-set").json()["status"] == "frozen"


def test_model_details_and_relations_prefer_the_immutable_weight_snapshot(collection_client, db_session):
    import json
    from datetime import date
    from app.models.factor_model import FactorModelRun
    from app.models.factor_weight_snapshot import FactorWeightSnapshot
    factor, version = _valid_factor(db_session)
    _create_member(collection_client, "model-source", factor, version)
    db_session.add(FactorModelRun(id="snapshot-model", status="validated",
        hyperparameters_json=json.dumps({"factor_set_id": "model-source"}),
        feature_versions_json=json.dumps({factor.code: {"version": 1,
            "coefficient": 0.99, "normalized_weight": 1.0}})))
    db_session.add(FactorWeightSnapshot(model_id="snapshot-model", factor_set_id="model-source",
        factor_ids_json=json.dumps([factor.id]), factor_version_ids_json=json.dumps([version.id]),
        roles_json='["feature"]', constraints_json='["free"]', missing_strategies_json='["exclude"]',
        data_cutoff_date=date(2026, 10, 1), train_mode="warehouse",
        weights_norm_json=json.dumps([{"factor_code": factor.code, "factor_version": 1,
            "factor_version_id": version.id, "coef_raw": -0.25, "weight_norm": -1.0,
            "training_ic": 0.04, "validation_ic": 0.03}])))
    db_session.commit()
    detail = collection_client.get("/api/v1/scoring/models/snapshot-model")
    assert detail.status_code == 200, detail.text
    weight = detail.json()["factors"][0]
    assert (weight["coefficient"], weight["normalized_weight"], weight["side"]) == (-0.25, -1.0, "short")
    relations = collection_client.get("/api/v1/scoring/models/snapshot-model/relations")
    assert relations.status_code == 200, relations.text
    relation = relations.json()["factors"][0]
    assert (relation["coef_raw"], relation["weight_norm"]) == (-0.25, -1.0)


def test_manual_fallback_returns_the_full_persisted_runtime(collection_client, db_session):
    from app.models.factor_model import FactorModelRun
    from app.models.factor_runtime import FactorRuntimeState
    db_session.add(FactorModelRun(id="old-model", status="validated"))
    db_session.flush()
    db_session.add(FactorRuntimeState(id=1, weight_mode="ridge", active_model_run_id="old-model", version=8))
    db_session.commit()
    response = collection_client.post("/api/v1/scoring/runtime/fallback", json={
        "actor": "contract-test", "reason": "Reviewed fallback",
    })
    assert response.status_code == 200, response.text
    runtime = response.json()
    assert {key: runtime.get(key) for key in ("weight_mode", "score_weight_mode", "version",
        "active_model_run_id", "fallback_reason", "updated_by")} == {
        "weight_mode": "manual", "score_weight_mode": "manual", "version": 9,
        "active_model_run_id": None, "fallback_reason": "Reviewed fallback", "updated_by": "contract-test",
    }
    assert runtime["updated_at"]


def test_collection_summary_reports_actual_member_version_qualification(collection_client, db_session):
    factor, version = _valid_factor(db_session)
    _create_member(collection_client, "qualification-set", factor, version)
    summary = collection_client.get("/api/v1/scoring/factorsets").json()[0]
    assert summary["members"][0].get("version_trainable") is True
    version.validation_status = "pending"
    db_session.commit()
    summary = collection_client.get("/api/v1/scoring/factorsets").json()[0]
    assert summary["members"][0].get("version_trainable") is False
    assert summary["members"][0]["version_status"] == "pending"


def test_freezing_returns_complete_members_so_refresh_failure_cannot_erase_them(collection_client, db_session):
    factor, version = _valid_factor(db_session)
    _create_member(collection_client, "freeze-result", factor, version)
    response = collection_client.post("/api/v1/scoring/factorsets/freeze-result/freeze", json={"reason": "Reviewed inputs"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "frozen"
    assert result["member_count"] == 1
    assert result["n_members"] == 1
    assert result["members"][0]["factor_code"] == factor.code
    assert result["members"][0]["version_trainable"] is True
