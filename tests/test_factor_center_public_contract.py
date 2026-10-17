"""Factor-center public contracts; all artifacts live in isolated test stores."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import factors, factor_evaluation, factor_shadow, scoring_facade
from app.db.session import get_db
from app.models.factor_runtime import FactorSystemConfig


@pytest.fixture
def center_client(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(tmp_path / "warehouse.duckdb"))
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1,
        warehouse_path=str(tmp_path / "warehouse.duckdb"), updated_by="isolated_test"))
    db_session.commit()
    app = FastAPI()
    for router in (factors.router, factor_evaluation.router, factor_shadow.router, scoring_facade.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def test_unconnected_shadow_writes_are_blocked_with_capability_reason(center_client):
    caps = center_client.get("/api/v1/factor-shadow/capabilities")
    assert caps.status_code == 200
    body = caps.json()
    assert body["observation_collection"]["supported"] is False
    assert body["activation"]["supported"] is False
    assert body["activation"]["reason"]
    for endpoint, payload in [
        ("observations", {"factor_id": 1, "factor_version_id": 1, "trade_date": "2020-01-01"}),
        ("activation/approve", {"factor_id": 1, "factor_version_id": 1, "reason": "manual"}),
    ]:
        response = center_client.post(f"/api/v1/factor-shadow/{endpoint}", json=payload)
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "shadow_collection_not_connected"


def _draft(client, code="audit_factor"):
    created = client.post("/api/v1/factors", json={"code": code,
        "name": code, "category": "technical"})
    assert created.status_code == 201, created.text
    return created.json()


def test_generic_transition_cannot_bypass_shadow_admission(center_client, db_session):
    from app.models.factor import Factor
    factor = _draft(center_client)
    row = db_session.get(Factor, factor["id"])
    row.lifecycle_status = "shadow"
    db_session.commit()
    response = center_client.post("/api/v1/factors/audit_factor/transitions",
        json={"action": "activate", "reason": "bypass"})
    assert response.status_code == 409
    assert "shadow_collection_not_connected" in response.text
    assert center_client.get("/api/v1/factors/audit_factor").json()["lifecycle_status"] == "shadow"


def test_version_listing_preserves_identity_and_compilation_evidence(center_client):
    _draft(center_client)
    created = center_client.post("/api/v1/factors/audit_factor/versions", json={
        "formula_expr": "close", "postprocess": {"rank": True},
    })
    assert created.status_code == 201, created.text
    version = created.json()
    listed = center_client.get("/api/v1/factors/audit_factor/versions").json()[0]
    assert listed["id"] == version["id"]
    assert listed["factor_id"] == version["factor_id"]
    assert listed["validation_status"] == version["validation_status"]
    assert listed["execution_plan_hash"] == version["execution_plan_hash"]
    assert listed["postprocess"] == {"rank": True}


@pytest.mark.parametrize("endpoint", ["preflight", "tasks"])
def test_explicit_foreign_version_is_rejected_without_falling_back(center_client, monkeypatch, endpoint):
    from app.services.factors import wp5_eval_task
    monkeypatch.setattr(wp5_eval_task, "_start_worker", lambda *args: None)
    _draft(center_client, "factor_a")
    _draft(center_client, "factor_b")
    for code in ("factor_a", "factor_b"):
        made = center_client.post(f"/api/v1/factors/{code}/versions", json={"formula_expr": "close"})
        assert made.status_code == 201
    response = center_client.post(f"/api/v1/factor-evaluation/{endpoint}", json={
        "factor_code": "factor_a", "factor_version_id": str(made.json()["id"]) if endpoint == "preflight" else made.json()["id"],
    })
    assert response.status_code in (400, 409)
    assert "factor_version_mismatch" in response.text


@pytest.mark.parametrize("endpoint", ["preflight", "tasks"])
def test_unconnected_index_universe_cannot_be_misreported_as_filtered(center_client, monkeypatch, endpoint):
    from app.services.factors import wp5_eval_task
    monkeypatch.setattr(wp5_eval_task, "_start_worker", lambda *args: None)
    _draft(center_client)
    center_client.post("/api/v1/factors/audit_factor/versions", json={"formula_expr": "close"})
    response = center_client.post(f"/api/v1/factor-evaluation/{endpoint}", json={
        "factor_code": "audit_factor", "universe": "hs300",
    })
    assert response.status_code in (400, 409)
    assert "evaluation_universe_not_supported" in response.text


def test_factor_library_reports_actual_version_and_evaluation(center_client, db_session):
    from app.models.factor_evaluation import EvaluationRun
    _draft(center_client)
    version = center_client.post("/api/v1/factors/audit_factor/versions",
        json={"formula_expr": "close"}).json()
    before = center_client.get("/api/v1/scoring/factor-definitions").json()["items"][0]
    assert before["latest_version_id"] == version["id"]
    assert before["latest_version_no"] == 1
    assert before["latest_evaluation"] is None
    db_session.add(EvaluationRun(id="actual-eval", factor_version_id=version["id"],
        gate_result="rejected", metrics_json='{"ic_mean": -0.17}', production_fidelity=1))
    db_session.commit()
    after = center_client.get("/api/v1/scoring/factor-definitions").json()["items"][0]
    assert after["latest_evaluation"]["gate_result"] == "rejected"
    assert after["latest_evaluation"]["metrics"]["ic_mean"] == -0.17
    detail = center_client.get("/api/v1/scoring/factor-definitions/audit_factor").json()
    assert detail["latest_version_no"] == 1
    assert detail["versions"][0]["formula_expr"] == "close"


def test_usage_does_not_call_research_age_production_or_drop_model_weights(center_client, db_session):
    from datetime import datetime, timedelta
    from app.models.factor import Factor
    from app.models.factor_model import FactorModelRun
    from app.models.factor_weight_snapshot import FactorWeightSnapshot
    factor = _draft(center_client)
    row = db_session.get(Factor, factor["id"])
    row.created_at = datetime.now() - timedelta(days=40)
    db_session.add(FactorModelRun(id="usage-model", status="validated"))
    db_session.flush()
    db_session.add(FactorWeightSnapshot(model_id="usage-model", factor_set_id="historical-set",
        data_cutoff_date=datetime.now().date(), train_mode="warehouse", weights_norm_json=
        '[{"factor_code":"audit_factor","factor_version":1,"weight_norm":0.7}]'))
    db_session.commit()
    response = center_client.get("/api/v1/scoring/factors/audit_factor")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["days_in_production"] is None
    assert len(body["in_models"]) == 1
    assert body["in_models"][0]["normalized_weight"] == 0.7


def test_version_identity_includes_source_mapping_and_postprocessing(center_client):
    _draft(center_client)
    first = center_client.post("/api/v1/factors/audit_factor/versions", json={
        "formula_expr": "close", "source_mapping": {"close": "source-a"},
    }).json()
    second_response = center_client.post("/api/v1/factors/audit_factor/versions", json={
        "formula_expr": "close", "source_mapping": {"close": "source-b"},
    })
    assert second_response.status_code == 201
    second = second_response.json()
    assert second["id"] != first["id"]
    assert second["source_mapping"] == {"close": "source-b"}
    retry = center_client.post("/api/v1/factors/audit_factor/versions", json={
        "formula_expr": "close", "source_mapping": {"close": "source-b"},
    }).json()
    assert retry["id"] == second["id"]


def test_draft_review_cannot_promote_a_changed_upstream_formula(center_client, db_session):
    from app.models.custom_indicator import CustomIndicator
    indicator = CustomIndicator(name="Reviewed source", key="reviewed_source", formula="close",
        value_type="number", params_json="[]")
    db_session.add(indicator)
    db_session.commit()
    submitted = center_client.post("/api/v1/scoring/external-draft/submit", json={
        "source_module": "custom_indicators", "source_ref_id": indicator.id,
        "payload": {"code": "reviewed_source", "name": "Reviewed source", "formula": "close"},
    })
    assert submitted.status_code == 200, submitted.text
    draft_no = submitted.json()["draft_id"]
    indicator.formula = "open"
    db_session.commit()
    reviewed = center_client.post(f"/api/v1/scoring/drafts/{draft_no}/approve",
        json={"reviewer": "local_user", "review_msg": "reviewed close formula"})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review_status"] == "submitted"
    assert "upstream_indicator_changed" in reviewed.json()["review_msg"]
    assert center_client.get("/api/v1/factors/reviewed_source").status_code == 404


def test_replayed_transition_is_idempotent_and_keys_cannot_cross_factors(center_client):
    for code in ("factor_a", "factor_b"):
        _draft(center_client, code)
    payload = {"action": "submit_candidate", "request_id": "same-key", "reason": "review"}
    first = center_client.post("/api/v1/factors/factor_a/transitions", json=payload)
    replay = center_client.post("/api/v1/factors/factor_a/transitions", json=payload)
    assert first.status_code == replay.status_code == 200
    assert replay.json()["audit_id"] == first.json()["audit_id"]
    wrong = center_client.post("/api/v1/factors/factor_b/transitions", json=payload)
    assert wrong.status_code == 409
    assert "transition_request_id_conflict" in wrong.text
    assert center_client.get("/api/v1/factors/factor_b").json()["lifecycle_status"] == "draft"


def test_version_references_do_not_accept_another_factors_version(center_client):
    _draft(center_client, "factor_a")
    _draft(center_client, "factor_b")
    version = center_client.post("/api/v1/factors/factor_b/versions", json={"formula_expr": "close"}).json()
    response = center_client.get(f"/api/v1/factors/factor_a/references?version_id={version['id']}")
    assert response.status_code in (400, 404)
    assert "factor_version_mismatch" in response.text


def test_draft_apply_database_failure_rolls_back_promoted_factor(center_client, db_session):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from app.models.custom_indicator import CustomIndicator
    from app.models.factor_governance import FactorDraft
    indicator = CustomIndicator(name="Atomic source", key="atomic_source", formula="close",
        value_type="number", params_json="[]")
    db_session.add(indicator)
    db_session.commit()
    submitted = center_client.post("/api/v1/scoring/external-draft/submit", json={
        "source_module": "custom_indicators", "source_ref_id": indicator.id,
        "payload": {"code": "atomic_source", "name": "Atomic source"},
    }).json()
    failed = False

    def reject_apply_write(session, *_args):
        nonlocal failed
        if any(isinstance(row, FactorDraft) and row.review_status == "applied"
               for row in session.dirty):
            failed = True
            raise RuntimeError("isolated apply write failure")

    event.listen(Session, "before_flush", reject_apply_write)
    try:
        response = center_client.post(f"/api/v1/scoring/drafts/{submitted['draft_id']}/approve",
            json={"reviewer": "local_user"})
    finally:
        event.remove(Session, "before_flush", reject_apply_write)
    assert failed
    assert response.status_code == 200, response.text
    assert response.json()["review_status"] == "submitted"
    assert center_client.get("/api/v1/factors/atomic_source").status_code == 404


def test_evaluation_task_round_trip_pins_version_and_cancels_safely(center_client, monkeypatch):
    import json
    from app.services.factors import wp5_eval_task
    monkeypatch.setattr(wp5_eval_task, "_start_worker", lambda *args: None)
    _draft(center_client)
    first_version = center_client.post("/api/v1/factors/audit_factor/versions",
        json={"formula_expr": "close"}).json()
    created = center_client.post("/api/v1/factor-evaluation/tasks",
        json={"factor_code": "audit_factor"})
    assert created.status_code == 200, created.text
    task = created.json()
    assert json.loads(task["payload_json"])["factor_version_id"] == first_version["id"]
    assert center_client.get(f"/api/v1/factor-evaluation/tasks/{task['id']}").json()["status"] == "queued"
    assert center_client.get(f"/api/v1/factor-evaluation/tasks/{task['id']}/heartbeat").status_code == 200
    assert any(row["id"] == task["id"] for row in center_client.get("/api/v1/factor-evaluation/tasks").json())
    assert center_client.post(f"/api/v1/factor-evaluation/tasks/{task['id']}/cancel").json()["status"] == "cancelled"
    center_client.post("/api/v1/factors/audit_factor/versions", json={"formula_expr": "open"})
    second = center_client.post("/api/v1/factor-evaluation/tasks", json={"factor_code": "audit_factor"}).json()
    assert second["id"] != task["id"]
    assert json.loads(second["payload_json"])["factor_version_id"] != first_version["id"]


def test_factor_task_route_cannot_read_or_cancel_an_unrelated_task(center_client, db_session):
    from app.models.async_task import AsyncTaskRecord
    other = AsyncTaskRecord(id="not-an-evaluation", task_type="data_mirror", status="queued", stage="queued")
    db_session.add(other)
    db_session.commit()
    base = "/api/v1/factor-evaluation/tasks/not-an-evaluation"
    assert center_client.get(base).status_code == 404
    assert center_client.post(base + "/cancel").status_code == 404


def test_evaluation_preflight_reads_the_configured_local_warehouse(center_client, db_session, tmp_path):
    from app.services.factors.store import FactorWarehouse
    warehouse = FactorWarehouse(tmp_path / "warehouse.duckdb")
    warehouse.initialize()
    with warehouse.safe_write_context() as connection:
        connection.execute("""INSERT INTO raw_daily_bars
            (symbol, trade_date, adjust, close, source, source_origin, source_row_id, ingested_at, batch_id)
            VALUES ('600001', '2026-01-05', 'qfq', 17.0, 'isolated', 'isolated', 1, now(), 'isolated')""")
    _draft(center_client)
    center_client.post("/api/v1/factors/audit_factor/versions", json={"formula_expr": "close"})
    response = center_client.post("/api/v1/factor-evaluation/preflight", json={"factor_code": "audit_factor"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    fields = next(row for row in items if row["code"].startswith("preflight.data_dependencies"))
    assert fields["severity"] == "pass", fields
    market = next(row for row in items if row["code"].startswith("preflight.market_coverage"))
    assert market["evidence"]["latest_trade_date"] == "2026-01-05"


def test_usage_coverage_is_actual_recent_window_not_latest_day(center_client, tmp_path):
    from app.services.factors.store import FactorWarehouse
    warehouse = FactorWarehouse(tmp_path / "warehouse.duckdb")
    warehouse.initialize()
    _draft(center_client)
    center_client.post("/api/v1/factors/audit_factor/versions", json={"formula_expr": "close"})
    with warehouse.safe_write_context() as connection:
        for day in ("2026-01-05", "2026-01-06"):
            for symbol in ("600001", "600002"):
                connection.execute("""INSERT INTO raw_daily_bars
                    (symbol, trade_date, adjust, close, source, source_origin, source_row_id, ingested_at, batch_id)
                    VALUES (?, ?, 'qfq', 17.0, 'isolated', 'isolated', 1, now(), 'isolated')""", [symbol, day])
                eligible = not (day == "2026-01-06" and symbol == "600002")
                connection.execute("""INSERT INTO factor_values
                    (symbol, trade_date, factor_code, factor_version, raw_value, eligible, data_cutoff_at, calc_batch_id, created_at)
                    VALUES (?, ?, 'audit_factor', 1, 17.0, ?, now(), 'isolated', now())""", [symbol, day, eligible])
    response = center_client.get("/api/v1/scoring/factors/audit_factor")
    assert response.status_code == 200
    body = response.json()
    assert body["coverage_30d"] == 0.75
    assert body["coverage_window_days"] == 2
    assert body["coverage_as_of"] == "2026-01-06"
    assert body["ic_mean_30d"] is None


def test_saved_version_compilation_does_not_accept_unknown_data_fields(center_client):
    _draft(center_client)
    payload = {"formula_expr": "made_up_data + close"}
    validation = center_client.post("/api/v1/factors/validate", json=payload).json()
    assert validation["is_valid"] is False
    saved = center_client.post("/api/v1/factors/audit_factor/versions", json=payload)
    assert saved.status_code == 201
    assert saved.json()["validation_status"] == "invalid"
    assert saved.json()["validation_errors"]


def test_shadow_quarantine_checks_factor_version_ownership(center_client, db_session):
    from app.models.factor import Factor
    first = _draft(center_client, "factor_a")
    _draft(center_client, "factor_b")
    version = center_client.post("/api/v1/factors/factor_b/versions", json={"formula_expr": "close"}).json()
    db_session.get(Factor, first["id"]).lifecycle_status = "shadow"
    db_session.commit()
    response = center_client.post(f"/api/v1/factor-shadow/quarantine/{first['id']}", json={
        "factor_id": first["id"], "factor_version_id": version["id"], "reason": "review",
    })
    assert response.status_code == 422
    assert center_client.get("/api/v1/factors/factor_a").json()["lifecycle_status"] == "shadow"


def test_revalidating_legacy_hash_without_compilation_evidence_creates_new_version(center_client, db_session):
    from app.models.factor_model import FactorVersion
    from app.models.factor_evaluation import EvaluationRun
    _draft(center_client)
    payload = {"formula_expr": "close"}
    original = center_client.post("/api/v1/factors/audit_factor/versions", json=payload).json()
    legacy = db_session.get(FactorVersion, original["id"])
    legacy.validation_status = None
    legacy.compiler_version = None
    legacy.formula_ast_json = None
    legacy.data_dependencies_json = None
    db_session.add(EvaluationRun(id="legacy-reference", factor_version_id=legacy.id))
    db_session.commit()
    repaired = center_client.post("/api/v1/factors/audit_factor/versions", json=payload)
    assert repaired.status_code == 201
    assert repaired.json()["id"] != original["id"]
    assert repaired.json()["validation_status"] == "valid"
    assert repaired.json()["compiler_version"]
    versions = center_client.get("/api/v1/factors/audit_factor/versions").json()
    previous = next(row for row in versions if row["id"] == original["id"])
    assert previous["validation_status"] is None
    assert previous["formula_expr"] == "close"
