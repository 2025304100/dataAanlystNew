"""Rejected fits retain observable coefficients without gaining release rights."""
import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

pytestmark = pytest.mark.whitebox


@pytest.fixture
def rejected_weight_client(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(tmp_path / "read-weights.duckdb"))
    from app.api.routes import factor_models, scoring_facade
    from app.db.session import get_db
    from app.models.factor import Factor
    from app.models.factor_evaluation import FactorSet, FactorSetMember
    from app.models.factor_model import FactorModelRun, FactorVersion
    from app.models.factor_runtime import FactorSystemConfig

    factor = Factor(code="turnover_read", name="Observed turnover", direction="nonlinear",
        category="technical", status="active", source_type="manual", frequency="daily")
    db_session.add(factor)
    db_session.flush()
    version = FactorVersion(factor_id=factor.id, version=3, formula_expr="close",
        direction="nonlinear", params_json="{}", validation_status="valid")
    db_session.add(version)
    db_session.flush()
    frozen = FactorSet(id="rejected-read-set", name="Frozen inputs", status="frozen",
        content_hash="test-read-only-frozen-inputs")
    db_session.add(frozen)
    db_session.flush()
    db_session.add(FactorSetMember(factor_set_id=frozen.id, factor_id=factor.id,
        factor_version_id=version.id, factor_code=factor.code, factor_version=3,
        role="feature", missing_policy="exclude"))
    db_session.add(FactorModelRun(id="rejected-fit", status="rejected",
        rejection_reason="validation_ic:-0.002448<0.000000",
        feature_versions_json=json.dumps({factor.code: 3}),
        hyperparameters_json=json.dumps({"factor_set_id": frozen.id, "mode": "warehouse"}),
        metrics_json='{"validation_ic":-0.00244791,"train_ic":0.0122609}',
        sample_count=1234, trade_date_count=250, symbol_count=24))
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1,
        warehouse_path=str(tmp_path / "read-weights.duckdb"), updated_by="isolated_test"))
    db_session.commit()
    app = FastAPI()
    for router in (factor_models.router, scoring_facade.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client, version.id


@pytest.mark.parametrize("source", ["materialized", "snapshot", "missing", "wrong_version"])
def test_public_model_reads_preserve_real_weights_and_rejected_admission(
    rejected_weight_client, db_session, source,
):
    from app.models.factor_governance import FactorModelMember
    from app.models.factor_weight_snapshot import FactorWeightSnapshot

    client, version_id = rejected_weight_client
    if source != "missing":
        db_session.add(FactorModelMember(model_run_id="rejected-fit", factor_code="turnover_read",
            factor_version=2 if source == "wrong_version" else 3, coefficient=1.7822e-5,
            normalized_weight=1.0, train_ic=-0.0122609, validation_ic=0.00244791,
            coverage=0.98, side="long"))
    if source == "snapshot":
        db_session.add(FactorWeightSnapshot(model_id="rejected-fit", factor_set_id="rejected-read-set",
            weights_raw_json='{"turnover_read":0.25}', weights_norm_json=json.dumps([{
                "factor_code": "turnover_read", "factor_version": 3,
                "factor_version_id": version_id, "coef_raw": 0.25, "weight_norm": 0.75,
                "training_ic": 0.06, "validation_ic": 0.04,
            }]), train_mode="warehouse", data_cutoff_date=date(2026, 10, 14)))
    db_session.commit()
    writes = []
    engine = db_session.get_bind()

    def capture_write(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", capture_write)
    try:
        relations = client.get("/api/v1/scoring/models/rejected-fit/relations")
        detail = client.get("/api/v1/scoring/models/rejected-fit")
        original_detail = client.get("/api/v1/factor-models/rejected-fit")
        assert relations.status_code == detail.status_code == original_detail.status_code == 200
        row = relations.json()["factors"][0]
        assert row["factor_version_id"] == version_id and row["version_label"] == "3"
        assert relations.json()["status"] == detail.json()["status"] == "rejected"
        if source in {"missing", "wrong_version"}:
            assert all(row[key] is None for key in
                ("coef_raw", "weight_norm", "training_ic", "validation_ic"))
        else:
            expected = (0.25, 0.75, 0.06, 0.04) if source == "snapshot" else (
                1.7822e-5, 1.0, -0.0122609, 0.00244791)
            assert tuple(row[key] for key in ("coef_raw", "weight_norm", "training_ic", "validation_ic")) == pytest.approx(expected)
            factor = detail.json()["factors"][0]
            assert factor["factor_version"] == 3
            assert (factor["coefficient"], factor["normalized_weight"], factor["train_ic"], factor["validation_ic"]) == pytest.approx(expected)
        assert original_detail.json()["production_eligible"] is False
        assert original_detail.json()["readiness_code"] == "MODEL_NOT_VALIDATED"
        assert not writes, writes
    finally:
        event.remove(engine, "before_cursor_execute", capture_write)
    refused = client.post("/api/v1/scoring/models/rejected-fit/activate", json={"mode": "ridge"})
    assert refused.status_code in (400, 409), refused.text
    assert "only validated models can be activated, got rejected" in refused.text
    assert client.get("/api/v1/factor-models/runtime").json()["active_model_run_id"] is None


@pytest.mark.parametrize("evidence", ["same_batch", "wrong_batch", "wrong_version", "missing",
    "other_model", "non_finite", "out_of_range", "boolean"])
def test_public_model_coverage_uses_only_matching_model_batch_and_version(
    rejected_weight_client, db_session, monkeypatch, evidence,
):
    from types import SimpleNamespace
    from app.models.factor_model import FactorModelRun
    from app.models.factor_governance import FactorModelMember
    from app.services.factors import health

    client, _version_id = rejected_weight_client
    model = db_session.get(FactorModelRun, "rejected-fit")
    hp = json.loads(model.hyperparameters_json)
    hp["factor_calc_batch_id"] = "own-frozen-batch"
    entry = {"factor_calc_batch_id": "own-frozen-batch", "factor_version": 3,
             "coverage": 0.9890963628}
    if evidence == "wrong_batch":
        entry["factor_calc_batch_id"] = "old-batch"
    elif evidence == "wrong_version":
        entry["factor_version"] = 2
    elif evidence == "non_finite":
        entry["coverage"] = float("nan")
    elif evidence == "out_of_range":
        entry["coverage"] = 1.5
    elif evidence == "boolean":
        entry["coverage"] = True
    if evidence not in {"missing", "other_model"}:
        hp["training_gate_evidence"] = {"turnover_read": entry}
    if evidence == "other_model":
        db_session.add(FactorModelRun(id="another-fit", status="validated",
            feature_versions_json='{"turnover_read":3}', hyperparameters_json=json.dumps({
                "factor_set_id": "rejected-read-set", "factor_calc_batch_id": "own-frozen-batch",
                "training_gate_evidence": {"turnover_read": entry},
            })))
    model.hyperparameters_json = json.dumps(hp)
    db_session.add(FactorModelMember(model_run_id=model.id, factor_code="turnover_read",
        factor_version=3, coefficient=0.2, normalized_weight=1.0))
    db_session.commit()
    calls = []

    def current_global_health(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(factors=[SimpleNamespace(factor_code="turnover_read", coverage=.874503)])

    monkeypatch.setattr(health, "get_factor_health", current_global_health)
    relations = client.get("/api/v1/scoring/models/rejected-fit/relations")
    detail = client.get("/api/v1/scoring/models/rejected-fit")
    assert relations.status_code == detail.status_code == 200
    wanted = 0.9890963628 if evidence == "same_batch" else None
    assert relations.json()["factors"][0]["coverage"] == wanted
    assert detail.json()["factors"][0]["coverage"] == wanted
    assert not calls, "A model's frozen coverage must not read current global health"
