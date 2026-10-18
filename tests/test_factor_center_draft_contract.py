"""Audit-confirmed factor creation never depends on a real model or provider."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.whitebox


@pytest.fixture
def draft_client(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(tmp_path / "warehouse.duckdb"))
    from app.api.routes.ai_drafts import router as drafts_router
    from app.api.routes.scoring_facade import router as scoring_router
    from app.db.session import get_db
    app = FastAPI()
    app.include_router(drafts_router, prefix="/api/v1")
    app.include_router(scoring_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)


def _payload(**overrides):
    return {"code": "reviewed_factor", "name": "Reviewed factor", "category": "trend",
            "formula_expr": "mean(close,20)", "params": {}, "direction": "higher_better",
            "factor_kind": "continuous", "risk_level": "medium", **overrides}


def _audit(db, payload):
    from app.models.ai_session import AISession, AIMessage, AIActionAudit
    session = AISession(title="Isolated factor draft")
    db.add(session)
    db.flush()
    message = AIMessage(session_id=session.id, role="assistant", content="Fixture suggestion")
    db.add(message)
    db.flush()
    audit = AIActionAudit(message_id=message.id, action_type="draft_factor",
                          suggested_payload=json.dumps(payload), user_confirmed=False)
    db.add(audit)
    db.commit()
    return audit.id


def test_confirmed_invalid_formula_cannot_create_an_invalid_factor(draft_client, db_session):
    audit_id = _audit(db_session, _payload(formula_expr="missing_function(close)"))
    preview = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/preview", json={})
    assert preview.status_code == 200 and preview.json()["is_valid"] is False
    confirmed = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    executed = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/execute", json={})
    assert executed.status_code == 200, executed.text
    assert executed.json()["success"] is False
    assert executed.json()["error"] == "validation_failed"
    assert draft_client.get("/api/v1/scoring/factor-definitions/reviewed_factor").status_code == 404


def test_failed_factor_execution_can_be_corrected_and_retried_but_success_stays_immutable(draft_client, db_session):
    original = _payload(formula_expr="missing_function(close)")
    audit_id = _audit(db_session, original)
    assert draft_client.post(f"/api/v1/ai/drafts/{audit_id}/confirm", json={}).status_code == 200
    failed = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/execute", json={})
    assert failed.json()["success"] is False
    corrected = _payload(params={"n": 20}, formula_expr="mean(close,20)",
                         postprocess={"rank": {"method": "percentile"}, "missing_policy": "exclude"})
    preview = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/preview", json={"modified_payload": corrected})
    assert preview.json()["is_valid"] is True, preview.text
    reconfirmed = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/confirm", json={"modified_payload": corrected})
    assert reconfirmed.status_code == 200, reconfirmed.text
    assert reconfirmed.json()["original_suggested_payload"] == original
    created = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/execute", json={})
    assert created.status_code == 200 and created.json()["success"] is True, created.text
    assert created.json()["lifecycle_status"] == "draft"
    definition = draft_client.get("/api/v1/scoring/factor-definitions/reviewed_factor").json()
    saved = definition["versions"][0]
    assert saved["params"] == {"n": 20}
    assert saved["postprocess"] == corrected["postprocess"]
    assert saved["validation_status"] == "valid"
    repeated = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/execute", json={})
    assert repeated.json()["factor_id"] == created.json()["factor_id"]
    assert repeated.json()["factor_version_id"] == created.json()["factor_version_id"]
    blocked = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/confirm", json={"modified_payload": _payload(code="another_factor")})
    assert blocked.status_code == 409


def test_factor_preview_and_execution_validate_postprocessing_as_part_of_the_payload(draft_client, db_session):
    audit_id = _audit(db_session, _payload(postprocess={"missing_policy": "invented_policy"}))
    preview = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/preview", json={})
    assert preview.status_code == 200 and preview.json()["is_valid"] is False, preview.text
    assert draft_client.post(f"/api/v1/ai/drafts/{audit_id}/confirm", json={}).status_code == 200
    executed = draft_client.post(f"/api/v1/ai/drafts/{audit_id}/execute", json={})
    assert executed.json()["success"] is False
    assert draft_client.get("/api/v1/scoring/factor-definitions/reviewed_factor").status_code == 404
