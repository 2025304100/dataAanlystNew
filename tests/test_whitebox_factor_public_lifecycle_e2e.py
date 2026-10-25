"""Public user workflow with real SQL mirrors, formulas, labels, Ridge and scores.

Only worker dispatch is made synchronous. All model/data artifacts belong to
temporary SQLite and DuckDB files; this never touches the running instance.
"""
from datetime import date, timedelta
import json
import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import factor_sets, scores, scoring_facade
from app.db.session import get_db
from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_governance import ScoringLineage, FactorSetSnapshot
from app.models.factor_model import FactorModelRun, FactorVersion
from app.models.factor_runtime import FactorRuntimeState, FactorSystemConfig
from app.models.score import Score
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services.factors import pipeline_task
from app.services.factors.definitions import seed_factor_definitions
from app.services.factors.store import FactorWarehouse
from app.services.factors.batch_audit import audit_batch_atomicity
from app.services.factor_model_contract import (
    assess_factor_model_readiness, assess_factor_score_publication_readiness,
)

pytestmark = pytest.mark.whitebox


@pytest.fixture
def lifecycle_client(db_session, monkeypatch):
    app = FastAPI()
    for router in (factor_sets.router, scoring_facade.router, scores.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr(pipeline_task, "_start_worker", lambda task_id, runner: runner(task_id))
    # Dispatch is external; keep the real pipeline in the fixture process so input adapters apply.
    monkeypatch.setattr(pipeline_task, "_run_factor_pipeline_process",
        lambda task_id, **kwargs: pipeline_task._run_factor_pipeline(task_id))
    with TestClient(app) as client:
        yield client


def _draft(db, *, code="ep_ttm", status="valid"):
    seed_factor_definitions(db)
    factor = db.scalar(select(Factor).where(Factor.code == code))
    original = db.scalar(select(FactorVersion).where(
        FactorVersion.factor_id == factor.id, FactorVersion.version == 1,
    ))
    version = FactorVersion(factor_id=factor.id, version=2,
        formula_expr=original.formula_expr, params_json=original.params_json,
        source_mapping_json=original.source_mapping_json, direction=original.direction,
        validation_status=status)
    db.add(version)
    db.commit()
    return version


@pytest.mark.parametrize("issue,expected", [
    ("missing", "NOT_FOUND"), ("draft", "NOT_FROZEN"),
    ("empty", "EMPTY_SET"), ("invalid_version", "MEMBER_VERSION_NOT_TRAINABLE"),
    ("version_mismatch", "MEMBER_FACTOR_VERSION_MISMATCH"),
    ("ready", "READY"),
])
def test_public_factorset_readiness_exposes_actual_training_gate(
    lifecycle_client, db_session, issue, expected,
):
    version = _draft(db_session, status="invalid" if issue == "invalid_version" else "valid")
    set_id = "readiness-user-set"
    if issue != "missing":
        db_session.add(FactorSet(id=set_id, name="User set",
            status="draft" if issue == "draft" else "frozen", content_hash="a" * 64))
        db_session.flush()
        if issue != "empty":
            db_session.add(FactorSetMember(factor_set_id=set_id, factor_id=version.factor_id,
                factor_version_id=version.id, factor_code="ep_ttm",
                factor_version=1 if issue == "version_mismatch" else 2, role="feature"))
        db_session.commit()
    response = lifecycle_client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["factor_set_id"] == set_id
    assert body["code"] == expected
    assert body["ready"] == (issue == "ready")
    assert body["message"]
    assert isinstance(body["details"], dict)


def _seed_local_market(db, tmp_path, *, day_count=100, symbol_count=24):
    warehouse_path = tmp_path / "public-lifecycle.duckdb"
    db.add(FactorSystemConfig(id=1, feature_enabled=1, warehouse_path=str(warehouse_path),
        extra_json=json.dumps({"gates": {"coverage_threshold": .9,
            "ic_min": 0.0, "ic_max": 1.01, "min_sample_count": 500}}),
        updated_by="isolated_test"))
    # The deterministic signal is deliberately learnable; its high IC is
    # fixture behavior, not claimed real-market performance. Overrides stay
    # inside this temporary configuration; the model fit itself is real.
    days, current = [], date(2025, 1, 2)
    while len(days) < day_count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    symbol_ids = []
    for index in range(symbol_count):
        code = f"{index + 1:06d}"
        symbol = Symbol(symbol=code, name=code, asset_type="stock", market="CN", is_active=1)
        universe = UniverseSymbol(symbol=code, name=code, asset_type="stock", region="cn",
            market="sz", is_synced=1, last_bar_date=days[-1], bar_count=len(days))
        db.add_all([symbol, universe])
        db.flush()
        symbol_ids.append(symbol.id)
        beta = (index - 11.5) / 24
        previous = 10.0
        for day_index, day in enumerate(days):
            close = previous * math.exp(.002 * beta)
            db.add(UniverseDailyBar(universe_symbol_id=universe.id, trade_date=day,
                open=previous, high=max(previous, close) * 1.01, low=min(previous, close) * .99,
                close=close, volume=1e6, amount=close * 1e6,
                turnover_rate=1 + beta + (day_index % 7) / 10, source="isolated.market"))
            db.add(StockValuation(symbol_id=symbol.id, trade_date=day,
                pe_ttm=1 / (.05 + .015 * beta), pb=2, source="isolated.market"))
            previous = close
    db.commit()
    watchlist = Watchlist(name="Lifecycle scoped recipient", list_type="core")
    db.add(watchlist)
    db.flush()
    db.add(WatchlistItem(watchlist_id=watchlist.id, symbol_id=symbol_ids[0], status="watching"))
    db.commit()
    return warehouse_path, days, symbol_ids, watchlist.id


@pytest.mark.parametrize("activation_mode", ["ridge", "shadow"])
def test_real_public_freeze_train_activate_and_scoped_score_chain(
    lifecycle_client, db_session, tmp_path, activation_mode,
):
    client = lifecycle_client
    version = _draft(db_session)
    warehouse_path, days, symbol_ids, watchlist_id = _seed_local_market(db_session, tmp_path)
    created = client.post("/api/v1/factor-sets", json={"name": "User trained real model"})
    assert created.status_code == 200, created.text
    set_id = created.json()["id"]
    member = client.post(f"/api/v1/factor-sets/{set_id}/members", json={
        "factor_code": "ep_ttm", "factor_version": 2, "role": "feature", "missing_policy": "exclude",
    })
    assert member.status_code == 200, member.text
    assert member.json()["factor_version_id"] == version.id
    frozen = client.post(f"/api/v1/scoring/factorsets/{set_id}/freeze", json={
        "actor": "isolated_user", "reason": "Review immutable training inputs",
    })
    assert frozen.status_code == 200, frozen.text
    readiness = client.get(f"/api/v1/scoring/factorsets/{set_id}/readiness")
    assert readiness.status_code == 200 and readiness.json()["ready"] is True

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
    db_session.expire_all()
    state = db_session.get(FactorRuntimeState, 1)
    assert state is None or state.active_model_run_id is None
    model = db_session.get(FactorModelRun, model_id)
    assert len(model.weights) == 1 and model.weights[0].factor_version == 2
    assert model.sample_count > 500 and json.loads(model.metrics_json)["validation_ic"] > .9
    models = client.get("/api/v1/scoring/models", params={"scope": "production"})
    assert models.status_code == 200, models.text
    assert any(row["id"] == model_id and row["production_eligible"] for row in models.json())

    activated = client.post(f"/api/v1/scoring/models/{model_id}/activate", json={
        "mode": activation_mode, "actor": "isolated_user", "note": "Reviewed real model",
    })
    assert activated.status_code == 200, activated.text
    assert activated.json()["active_model_run_id"] == model_id
    overview = client.get("/api/v1/scoring/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["runtime_mode"] == activation_mode
    assert overview.json()["runtime_active_model_id"] == model_id
    assert overview.json()["score_weight_mode"] == ("manual" if activation_mode == "shadow" else "ridge")
    assert overview.json()["weight_mode"] == ("manual" if activation_mode == "shadow" else "ridge")
    assert overview.json()["active_model_id"] == (None if activation_mode == "shadow" else model_id)
    db_session.expire_all()
    assert assess_factor_score_publication_readiness(
        db_session, model_run_id=model_id, expected_weight_mode=activation_mode,
    ).ready
    assert not assess_factor_score_publication_readiness(
        db_session, model_run_id=model_id, expected_weight_mode="manual",
    ).ready
    assert not assess_factor_score_publication_readiness(
        db_session, model_run_id=model_id,
        expected_weight_mode="shadow" if activation_mode == "ridge" else "ridge",
    ).ready
    assert assess_factor_model_readiness(
        db_session, model_run_id=model_id, require_runtime_active=True,
    ).ready == (activation_mode == "ridge")
    scoring = client.post("/api/v1/scoring/tasks", json={
        "train_model": False, "materialize_scores": True,
        "start_date": days[-2].isoformat(), "end_date": days[-1].isoformat(),
        "universe_source": "watchlist", "watchlist_id": watchlist_id,
        "hyperparameters": {"expected_model_run_id": model_id,
            "expected_weight_mode": activation_mode}, "actor": "isolated_user",
    })
    assert scoring.status_code == 200, scoring.text
    scored_task = client.get(f"/api/v1/scoring/tasks/{scoring.json()['id']}").json()
    assert scored_task["status"] == "done", scored_task
    scored_result = scored_task["result"]
    assert scored_result["completion_status"] == "COMPLETED", scored_result
    db_session.expire_all()
    score_rows = db_session.scalars(select(Score).where(Score.factor_model_run_id == model_id)).all()
    assert len(score_rows) == 2
    for score in score_rows:
        assert score.symbol_id == symbol_ids[0]
        assert score.factor_set_id == set_id
        assert score.weight_mode == activation_mode
        assert score.model_alpha_score is not None
    lineages = db_session.scalars(select(ScoringLineage).where(ScoringLineage.model_run_id == model_id)).all()
    assert len(lineages) == 2
    assert all(json.loads(row.stats_json)["pipeline_run_id"] == scored_task["id"] for row in lineages)
    assert all(db_session.get(FactorSetSnapshot, row.factor_set_snapshot_id).factor_set_id == set_id for row in lineages)
    assert all(json.loads(row.factor_version_ids_json)["ep_ttm"]["version"] == 2 for row in lineages)
    if activation_mode == "shadow":
        # Shadow scores are inspectable evidence, but are not the trading
        # score scope. Explicit promotion must occur before formal reads.
        promoted = client.post(f"/api/v1/scoring/models/{model_id}/activate", json={
            "mode": "ridge", "actor": "isolated_user", "note": "Reviewed shadow evidence",
        })
        assert promoted.status_code == 200, promoted.text
        published = client.post("/api/v1/scoring/tasks", json={
            "train_model": False, "materialize_scores": True,
            "start_date": days[-2].isoformat(), "end_date": days[-1].isoformat(),
            "universe_source": "watchlist", "watchlist_id": watchlist_id,
            "hyperparameters": {"expected_model_run_id": model_id, "expected_weight_mode": "ridge"},
            "actor": "isolated_user",
        })
        assert published.status_code == 200, published.text
        formal_task = client.get(f"/api/v1/scoring/tasks/{published.json()['id']}").json()
        assert formal_task["status"] == "done", formal_task
        assert formal_task["result"]["completion_status"] == "COMPLETED"
    db_session.expire_all()
    latest = client.get(f"/api/v1/scores/latest/{symbol_ids[0]}")
    assert latest.status_code == 200, latest.text
    assert latest.json()["factor_model_run_id"] == model_id
    assert latest.json()["factor_set_id"] == set_id
    assert latest.json()["weight_mode"] == "ridge"
    assert latest.json()["model_alpha_score"] is not None
    history = client.get(f"/api/v1/scores/history/{symbol_ids[0]}")
    assert history.status_code == 200 and len(history.json()) == 2
    assert all(row["factor_model_run_id"] == model_id and row["weight_mode"] == "ridge" for row in history.json())
    warehouse = FactorWarehouse(warehouse_path)
    with warehouse.connection(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_daily_bars").fetchone()[0] == 2400
        assert connection.execute("SELECT COUNT(*) FROM raw_valuation_snapshots").fetchone()[0] == 2400
        assert connection.execute("SELECT COUNT(*) FROM factor_values WHERE factor_code='ep_ttm' AND factor_version=2").fetchone()[0] > 0


def test_two_public_refreshes_keep_shared_labels_and_independent_target_audits(
    lifecycle_client, db_session, tmp_path, caplog,
):
    warehouse_path, days, _, _ = _seed_local_market(
        db_session, tmp_path, day_count=8, symbol_count=3,
    )
    task_ids = []
    for start in (days[0], days[-2]):
        started = lifecycle_client.post("/api/v1/scoring/tasks", json={
            "train_model": False, "materialize_scores": False,
            "start_date": start.isoformat(), "end_date": days[-1].isoformat(),
            "actor": "isolated_user",
        })
        assert started.status_code == 200, started.text
        task = lifecycle_client.get(f"/api/v1/scoring/tasks/{started.json()['id']}").json()
        assert task["status"] == "done", task
        assert task["result"]["targets"]["calc_batch_id"] == "targets-labels"
        assert task["result"]["targets"]["audit_batch_id"] != "targets-labels"
        task_ids.append(task["id"])
    assert len(set(task_ids)) == 2
    warehouse = FactorWarehouse(warehouse_path)
    with warehouse.connection(read_only=True) as connection:
        audits = connection.execute("""SELECT batch_id, scope_json, status, rows_written
            FROM ingestion_batches WHERE source_key='target.generation'
            ORDER BY started_at""").fetchall()
        assert len(audits) == 2, audits
        assert connection.execute("SELECT COUNT(*) FROM factor_targets WHERE calc_batch_id='targets-labels'").fetchone()[0] == 24
        assert connection.execute("SELECT COUNT(DISTINCT calc_batch_id) FROM factor_targets").fetchone()[0] == 1
        coverage = connection.execute("SELECT batch_id, rows_total FROM warehouse_label_coverage").fetchall()
        assert coverage == [("targets-labels", 24)]
    assert len({audit[0] for audit in audits}) == 2
    assert {json.loads(audit[1])["pipeline_run_id"] for audit in audits} == set(task_ids)
    for audit_id, raw_scope, status, rows_written in audits:
        scope = json.loads(raw_scope)
        assert audit_id != "targets-labels"
        assert scope["data_calc_batch_id"] == "targets-labels"
        assert scope["start_date"] in {days[0].isoformat(), days[-2].isoformat()}
        assert scope["end_date"] == days[-1].isoformat()
        expected_rows = 24 if scope["start_date"] == days[0].isoformat() else 6
        assert status == "committed" and rows_written == expected_rows
        checked = audit_batch_atomicity(warehouse, batch_id=audit_id)
        assert checked.atomicity_evidence["data_calc_batch_id"] == "targets-labels"
        assert checked.actual_rows_in_table == expected_rows
        assert checked.atomicity_evidence["match"] is True
    assert not any("begin_batch failed for targets-labels" in record.message for record in caplog.records)

