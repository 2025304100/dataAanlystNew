"""Guarded turnover v3 through public APIs, using entirely temporary stores.

Worker dispatch alone is synchronous. Mirrors, the guarded DSL, target labels,
Ridge fit, immutable model lineage and score publication use real implementations.
The intentionally learnable fixture is not a claim about market performance.
"""
from __future__ import annotations

import json
import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

pytestmark = pytest.mark.whitebox


FORMULA = (
    "iif(count(turnover_rate - turnover_rate, 20) == 20, "
    "iif(stddev(turnover_rate, 20) > 1e-8, "
    "(turnover_rate - mean(turnover_rate, 20)) / stddev(turnover_rate, 20), 0), None)"
)


@pytest.fixture
def turnover_client(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "ai.json"))
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(tmp_path / "guarded-turnover.duckdb"))
    from app.api.routes import factors, factor_sets, factor_models, scoring_facade, scores
    from app.db.session import get_db
    from app.services.factors import pipeline_task
    app = FastAPI()
    for router in (factors.router, factor_sets.router, factor_models.router,
                   scoring_facade.router, scores.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    # This fixture runs the real pipeline inline so temporary input roots and
    # SQL guards stay in this test process. Process supervision has its own suite.
    monkeypatch.setattr(pipeline_task, "_start_worker",
                        lambda task_id, _runner: pipeline_task._run_factor_pipeline(task_id))
    with TestClient(app) as client:
        yield client


def _seed_turnover_market(db, tmp_path):
    from app.models.universe import UniverseDailyBar, UniverseSymbol
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    _warehouse_path, days, symbol_ids, watchlist_id = _seed_local_market(db, tmp_path)
    # Three equally sized groups have negative, zero and positive Z-scores.
    # Future returns depend on that signal, so the genuine fit can validate.
    for index, universe in enumerate(db.scalars(select(UniverseSymbol).order_by(UniverseSymbol.symbol))):
        slope = index % 3 - 1
        previous = 10.0
        bars = db.scalars(select(UniverseDailyBar).where(
            UniverseDailyBar.universe_symbol_id == universe.id,
        ).order_by(UniverseDailyBar.trade_date)).all()
        for day_index, bar in enumerate(bars):
            close = previous * math.exp(.001 * slope)
            bar.open, bar.close = previous, close
            bar.high, bar.low = max(previous, close) * 1.01, min(previous, close) * .99
            bar.amount = close * 1e6
            bar.turnover_rate = 10 + slope * .02 * (day_index + 1)
            previous = close
    db.commit()
    return days, symbol_ids, watchlist_id


def test_guarded_v3_freezes_trains_waits_for_confirmation_then_scores_in_isolation(
    turnover_client, db_session, tmp_path,
):
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion
    from app.services.factors.definitions import seed_factor_definitions
    seed_factor_definitions(db_session)
    factor = db_session.scalar(select(Factor).where(Factor.code == "turnover_z20"))
    legacy = FactorVersion(factor_id=factor.id, version=2,
        formula_expr="(turnover - mean_20d) / stddev_pop_20d",
        params_json='{"minimum_periods":20,"window":20}', direction="nonlinear",
        source_mapping_json='{"table":"raw_daily_bars","field":"turnover_rate"}',
        compiler_version="wp2-1.0.0", validation_status="valid", is_latest=1)
    db_session.add(legacy)
    db_session.commit()
    days, symbol_ids, watchlist_id = _seed_turnover_market(db_session, tmp_path)
    client = turnover_client
    old = client.get("/api/v1/factors/turnover_z20/versions").json()[0]
    created = client.post("/api/v1/factors/turnover_z20/versions", json={
        "formula_expr": FORMULA, "params": {}, "direction": "nonlinear",
        "source_mapping": {"table": "raw_daily_bars", "field": "turnover_rate"},
        "postprocess": {"winsorize": {"method": "mad", "mad_multiplier": 3.0},
                        "zscore": {"ddof": 0}, "missing_policy": "exclude"},
        "change_note": "Explicit finite-window DSL and population normalization contract",
        "created_by": "isolated_user",
    })
    assert created.status_code == 201, created.text
    version = created.json()
    assert version["version"] == 3 and version["validation_status"] == "valid"
    assert version["direction"] == "nonlinear" and version["params"] == {}
    historical = next(row for row in client.get("/api/v1/factors/turnover_z20/versions").json()
                      if row["id"] == old["id"])
    for key in ("formula_expr", "params", "source_mapping", "direction", "compiler_version", "validation_status"):
        assert historical[key] == old[key]
    new_set = client.post("/api/v1/factor-sets", json={"name": "Guarded turnover v3"})
    assert new_set.status_code == 200, new_set.text
    set_id = new_set.json()["id"]
    member = client.post(f"/api/v1/factor-sets/{set_id}/members", json={
        "factor_code": "turnover_z20", "factor_version": 3,
        "role": "feature", "missing_policy": "exclude",
    })
    assert member.status_code == 200 and member.json()["factor_version_id"] == version["id"]
    frozen = client.post(f"/api/v1/scoring/factorsets/{set_id}/freeze", json={"reason": "Reviewed immutable v3 inputs"})
    assert frozen.status_code == 200, frozen.text
    assert client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness").json()["ready"] is True

    trained = client.post("/api/v1/scoring/tasks", json={
        "train_model": True, "materialize_scores": True, "factor_set_id": set_id,
        "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
        "window_days": 90, "validation_days": 25,
        "hyperparameters": {"factor_set_id": set_id}, "factor_scope": {"factor_set_id": set_id},
        "actor": "isolated_user",
    })
    assert trained.status_code == 200, trained.text
    task = client.get(f"/api/v1/scoring/tasks/{trained.json()['id']}").json()
    assert task["status"] == "done", task
    result = task["result"]
    assert result["model"]["status"] == "validated", result
    assert result["completion_status"] == "AWAITING_MODEL_ACTIVATION"
    assert result["trained_model_auto_activated"] is False
    assert result["scoring_bridge"]["materialized_count"] == 0
    model_id = result["model"]["model_run_id"]
    runtime = client.get("/api/v1/factor-models/runtime").json()
    assert runtime["weight_mode"] == "manual" and runtime["active_model_run_id"] is None
    detail = client.get(f"/api/v1/scoring/models/{model_id}").json()
    assert detail["sample_count"] > 500
    assert [(row["factor_code"], row["factor_version"]) for row in detail["factors"]] == [("turnover_z20", 3)]
    relations = client.get(f"/api/v1/scoring/models/{model_id}/relations").json()
    assert relations["factor_set_id"] == set_id
    assert relations["factors"][0]["factor_version_id"] == version["id"]
    # The user-visible checkpoint must precede this explicit isolated approval.
    activated = client.post(f"/api/v1/scoring/models/{model_id}/activate", json={
        "mode": "ridge", "actor": "isolated_user", "note": "Reviewed this temporary model",
    })
    assert activated.status_code == 200, activated.text
    assert activated.json()["active_model_run_id"] == model_id
    scoring = client.post("/api/v1/scoring/tasks", json={
        "train_model": False, "materialize_scores": True,
        "start_date": days[-2].isoformat(), "end_date": days[-1].isoformat(),
        "universe_source": "watchlist", "watchlist_id": watchlist_id,
        "hyperparameters": {"expected_model_run_id": model_id, "expected_weight_mode": "ridge"},
        "actor": "isolated_user",
    })
    assert scoring.status_code == 200, scoring.text
    scoring_task = client.get(f"/api/v1/scoring/tasks/{scoring.json()['id']}").json()
    assert scoring_task["status"] == "done", scoring_task
    assert scoring_task["result"]["completion_status"] == "COMPLETED"
    latest = client.get(f"/api/v1/scores/latest/{symbol_ids[0]}")
    assert latest.status_code == 200, latest.text
    assert latest.json()["factor_model_run_id"] == model_id
    assert latest.json()["factor_set_id"] == set_id
    assert latest.json()["weight_mode"] == "ridge" and latest.json()["model_alpha_score"] is not None
    history = client.get(f"/api/v1/scores/history/{symbol_ids[0]}").json()
    assert len(history) == 2
    assert all(row["factor_model_run_id"] == model_id and row["factor_set_id"] == set_id for row in history)
