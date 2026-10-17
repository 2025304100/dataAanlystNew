"""Repeated public worker evaluation must preserve an explicit manual version grade."""
from contextlib import contextmanager

import pytest

from app.api.routes import factor_sets
from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from test_factor_mining_routes import _client
from test_m1_closure import mining_env, warehouse_path
from test_mining_runtime_integrity import _inputs, _seed, _wait

pytestmark = pytest.mark.whitebox


def test_repeated_real_worker_preserves_manual_grade_snapshot_and_set_membership(
        mining_env, warehouse_path, monkeypatch):
    db = mining_env
    _seed(db)
    client = _client(db)

    @contextmanager
    def isolated_set_session():
        yield db

    monkeypatch.setattr(factor_sets, "SessionLocal", isolated_set_session)
    client.app.include_router(factor_sets.router)
    payload = _inputs(warehouse_path, finalize=1)
    payload["evolution_params"]["ai_enabled"] = False

    def run_once(formula_hash=None):
        created = client.post("/factor-mining/runs", json=payload)
        assert created.status_code == 201, created.text
        run_id = created.json()["run_id"]
        detail = _wait(client, db, run_id, lambda run: run["status"] in ("succeeded", "failed"))
        assert detail["status"] == "succeeded", detail
        candidates = client.get(f"/factor-mining/runs/{run_id}/candidates").json()["items"]
        return next(candidate for candidate in candidates if candidate["latest_evaluation_id"]
            and (formula_hash is None or candidate["formula_hash"] == formula_hash))

    first = run_once()
    manual = client.post(f"/factor-mining/candidates/{first['id']}/grade/manual",
        json={"grade": "S", "reason": "人工复核同一版本并明确保留当前评级结论"})
    assert manual.status_code == 200, manual.text
    original = client.get(f"/factor-mining/candidates/{first['id']}/grade/evidence").json()

    # A real active reference identical to this candidate makes the next automatic
    # outcome D (correlation=1), so accidental governance would remove the member.
    baseline = Factor(code="manual_authority_reference", name="Manual grade reference",
        category="trend", direction="positive", status="active", lifecycle_status="active", is_active=1)
    db.add(baseline)
    db.flush()
    reference = FactorVersion(factor_id=baseline.id, version=1,
        formula_expr=first["canonical_formula"], validation_status="valid")
    db.add(reference)
    db.flush()
    baseline.active_version_id = reference.id
    db.commit()
    version = db.get(FactorVersion, first["factor_version_id"])
    made_set = client.post("/factor-sets", json={"name": "人工评级保留验证"})
    assert made_set.status_code == 200, made_set.text
    set_id = made_set.json()["id"]
    added = client.post(f"/factor-sets/{set_id}/members", json={
        "factor_id": version.factor_id, "factor_version_id": version.id})
    assert added.status_code == 200, added.text

    # Change the split, so the immutable evaluation cache cannot reuse run one;
    # evaluate the full small population even if its ranking changes slightly.
    payload["train_ratio"] = .56
    payload["finalize_top_k"] = 6
    second = run_once(first["formula_hash"])
    assert second["factor_version_id"] == first["factor_version_id"]
    assert second["latest_evaluation_id"] != first["latest_evaluation_id"]
    final_eval = client.get(f"/factor-mining/evaluations/{second['latest_evaluation_id']}").json()
    assert final_eval["metrics"]["correlation"] == pytest.approx(1.0)
    latest = client.get(f"/factor-mining/candidates/{second['id']}/grade/evidence").json()
    assert latest["grade"] == "S"
    assert latest["manual_adjusted"] is True
    assert latest["reason_zh"] == original["reason_zh"]
    assert latest["metrics"] == original["metrics"]
    members = client.get(f"/factor-sets/{set_id}").json()["members"]
    assert [member["factor_version_id"] for member in members] == [version.id]

    restored = client.post(f"/factor-mining/candidates/{second['id']}/grade/restore-auto")
    assert restored.status_code == 200, restored.text
    automatic = client.get(f"/factor-mining/candidates/{second['id']}/grade/evidence").json()
    assert automatic["grade"] == "D"
    assert automatic["manual_adjusted"] is False
    assert client.get(f"/factor-sets/{set_id}").json()["members"] == []
