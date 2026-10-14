"""Mining result retention through public HTTP endpoints, with an isolated DB."""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routes import factor_experience, factor_mining, factor_sets, factors, scoring_facade
from app.db.session import get_db
from app.models.factor_mining import FactorMiningCandidate, FactorMiningRun


pytestmark = pytest.mark.whitebox


@pytest.fixture
def mining_client(db_session):
    app = FastAPI()
    app.include_router(factor_mining.router)
    app.include_router(factor_experience.router)
    app.include_router(factor_sets.router)
    app.include_router(factors.router)
    app.include_router(scoring_facade.router)

    def isolated_request_db():
        with Session(db_session.get_bind()) as session:
            yield session

    app.dependency_overrides[get_db] = isolated_request_db
    return TestClient(app)


def seed_candidate(db_session, *, candidate_id="retained-candidate"):
    db_session.add(FactorMiningRun(
        id="retained-run", status="succeeded", candidate_pool_snapshot_id="retained-snap",
        data_cutoff_at=datetime(2026, 9, 1), start_date=datetime(2025, 1, 1),
        end_date=datetime(2026, 9, 1), rebalance_frequency="weekly",
        split_method="ratio", split_algorithm_version="split-1.0.0",
    ))
    db_session.flush()
    db_session.add(FactorMiningCandidate(
        id=candidate_id, run_id="retained-run", formula_expr="mean(close,20)",
        canonical_formula="mean(close,20)", formula_hash="retained1234567890",
        category="trend", operation="elite", generation=1,
        generation_icir=0.4, expected_direction="positive", economic_logic="均线趋势",
    ))
    db_session.commit()


def test_rejection_survives_next_request(mining_client, db_session):
    seed_candidate(db_session)
    response = mining_client.post("/factor-mining/candidates/batch-review", json={
        "actions": [{"candidate_id": "retained-candidate", "action": "reject", "reason": "不采纳"}],
    })
    assert response.status_code == 200
    assert response.json()["counts"]["rejected"] == 1
    candidates = mining_client.get("/factor-mining/runs/retained-run/candidates").json()["items"]
    assert candidates[0]["elimination_status"] == "manual_rejected"
    assert candidates[0]["elimination_reason"] == "不采纳"


def test_saved_candidate_is_visible_in_template_configuration(mining_client, db_session):
    seed_candidate(db_session)
    response = mining_client.post("/factor-mining/candidates/retained-candidate/save-as-template")
    assert response.status_code == 200
    template_id = response.json()["template_id"]
    listing = mining_client.get("/factor-mining/templates?scope=personal").json()
    saved = next(item for item in listing["items"] if item["template_id"] == template_id)
    assert saved["rule_config"]["formula"] == "mean(close,20)"
    assert saved["rule_config"]["required_fields"] == ["close"]
    assert saved["rule_config"]["source_candidate_id"] == "retained-candidate"
    assert saved["rule_config"]["source_run_id"] == "retained-run"
    again = mining_client.post("/factor-mining/candidates/retained-candidate/save-as-template").json()
    assert again == {"template_id": template_id, "status": "already"}


def test_negative_governance_removes_prior_positive_from_sampling(mining_client):
    payload = {"formula_ast": {"type": "Expression", "body": {
        "type": "Call", "func": "mean", "args": [
            {"type": "Name", "id": "close"}, {"type": "Constant", "value": 20},
        ], "keywords": [],
    }}, "source": "manual", "category": "trend"}
    positive = mining_client.post("/factor-experience", json=payload)
    assert positive.status_code == 200
    experience_id = positive.json()["experience_id"]
    negative = mining_client.post("/factor-experience", json={**payload, "is_negative_sample": 1})
    assert negative.status_code == 200
    assert negative.json()["experience_id"] == experience_id
    assert mining_client.get(f"/factor-experience/{experience_id}").json()["is_negative_sample"] == 1
    assert mining_client.get("/factor-experience/sample?field_scope=close").json()["items"] == []
    # 重复正样本不得绕过已持久化的负样本治理。
    mining_client.post("/factor-experience", json=payload)
    assert mining_client.get("/factor-experience/sample?field_scope=close").json()["items"] == []


def test_template_controls_govern_real_initial_population(mining_client, db_session):
    from app.services.factors.mining import initial_population as IP
    from app.services.factors.mining import template_service as TPL

    mining_client.post("/factor-mining/templates/seed")
    templates = mining_client.get("/factor-mining/templates?limit=200").json()["items"]
    for template in templates:
        assert mining_client.post(f"/factor-mining/templates/{template['template_id']}/enabled",
                                  json={"enabled": 0}).status_code == 200
    personal = mining_client.post("/factor-mining/templates", json={
        "name": "均线20验证", "rule_config": {"formula": "mean(close,20)",
        "category": "trend", "params": {}, "required_fields": ["close"], "priority": 1},
    })
    assert personal.status_code == 201
    population = IP.build_initial_population(population_size=4, template_limit=1,
        selected_fields=["close"], enabled_categories=["trend"],
        templates=TPL.load_population_templates(db_session))
    assert [item["formula"] for item in population.candidates] == ["mean(close,20)"]
    mining_client.post(f"/factor-mining/templates/{personal.json()['template_id']}/enabled",
                       json={"enabled": 0})
    population = IP.build_initial_population(population_size=4, template_limit=1,
        selected_fields=["close"], enabled_categories=["trend"],
        templates=TPL.load_population_templates(db_session))
    assert population.candidates == []


def test_template_missing_parameter_is_rejected_before_persistence(mining_client):
    response = mining_client.post("/factor-mining/templates", json={
        "name": "缺参数", "rule_config": {"formula": "mean(close,{n1})", "params": {}},
    })
    assert response.status_code == 422
    assert "n1" in response.json()["detail"]["detail_zh"]
    assert mining_client.get("/factor-mining/templates?scope=personal").json()["total"] == 0


def test_candidate_formula_version_and_frequency_reach_frozen_training_inputs(mining_client, db_session):
    seed_candidate(db_session)
    submitted = mining_client.post("/factor-mining/candidates/retained-candidate/submit")
    assert submitted.status_code == 200, submitted.text
    factor = mining_client.get(f"/factors/{submitted.json()['factor_code']}")
    assert factor.status_code == 200, factor.text
    assert factor.json()["frequency"] == "weekly"
    set_response = mining_client.post("/factor-sets", json={"name": "挖掘真实训练输入"})
    assert set_response.status_code == 200, set_response.text
    set_id = set_response.json()["id"]
    added = mining_client.post("/factor-mining/candidates/batch-review", json={"actions": [{
        "candidate_id": "retained-candidate", "action": "add_to_set", "factor_set_id": set_id,
    }]})
    assert added.json()["counts"]["added"] == 1, added.text
    frozen = mining_client.post(f"/factor-sets/{set_id}/freeze", json={"actor": "isolated-user"})
    assert frozen.status_code == 200, frozen.text
    member = frozen.json()["members"][0]
    assert member["factor_version_id"] == submitted.json()["factor_version_id"]
    versions = mining_client.get(f"/factors/{member['factor_code']}/versions").json()
    assert versions[0]["formula_expr"] == "mean(close,20)"
    readiness = mining_client.get(f"/scoring/factorsets/{set_id}/readiness")
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["ready"] is True, readiness.text
    assert mining_client.get("/factor-experience").json()["total"] == 1

