"""Training releases its own SQL read lease without ending caller transactions.

All database and warehouse inputs are isolated fixtures. The public task test
injects a database idle-timeout at the warehouse boundary instead of sleeping.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.routes import factor_sets, scoring_facade
from app.db.session import get_db
from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion
from app.models.factor_runtime import FactorSystemConfig
from app.models.symbol import Symbol
from app.services.factors import pipeline_task, ridge_model
from app.services.factors.definitions import seed_factor_definitions
from app.services.factors.store import FactorWarehouse
from tests.test_whitebox_factor_public_lifecycle_e2e import _draft, _seed_local_market


pytestmark = pytest.mark.whitebox


def test_public_training_survives_sql_idle_timeout_during_sample_loading(
    db_session, tmp_path, monkeypatch,
):
    _draft(db_session)
    _, days, _, _ = _seed_local_market(db_session, tmp_path)
    app = FastAPI()
    for router in (factor_sets.router, scoring_facade.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr(pipeline_task, "_start_worker", lambda task_id, runner: runner(task_id))
    # Dispatch is external; keep the real pipeline in the fixture process so input adapters apply.
    monkeypatch.setattr(pipeline_task, "_run_factor_pipeline_process",
        lambda task_id, **kwargs: pipeline_task._run_factor_pipeline(task_id))

    active_training = {}
    expired_leases = set()
    original_train = ridge_model.train_rolling_ridge
    original_samples = ridge_model._load_samples

    def train_with_idle_timeout(db, *args, **kwargs):
        active_training["db"] = db
        return original_train(db, *args, **kwargs)

    def load_samples_after_long_io(*args, **kwargs):
        db = active_training["db"]
        # A connection retained across a long warehouse read has exceeded the
        # server's idle timeout. Reacquiring a connection after I/O is healthy.
        if db.in_transaction():
            expired_leases.add(db.connection())
        active_training["sample_loads"] = active_training.get("sample_loads", 0) + 1
        return original_samples(*args, **kwargs)

    def reject_expired_lease(conn, cursor, statement, parameters, context, executemany):
        if conn in expired_leases:
            raise OperationalError(statement, parameters, RuntimeError("MySQL 2006: idle training lease expired"))

    monkeypatch.setattr(pipeline_task, "train_rolling_ridge", train_with_idle_timeout)
    monkeypatch.setattr(ridge_model, "_load_samples", load_samples_after_long_io)
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", reject_expired_lease)
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/factor-sets", json={"name": "SQL lease training"})
            assert created.status_code == 200, created.text
            set_id = created.json()["id"]
            member = client.post(f"/api/v1/factor-sets/{set_id}/members", json={
                "factor_code": "ep_ttm", "factor_version": 2,
                "role": "feature", "missing_policy": "exclude",
            })
            assert member.status_code == 200, member.text
            frozen = client.post(f"/api/v1/scoring/factorsets/{set_id}/freeze", json={
                "actor": "isolated_user", "reason": "Immutable SQL lease test inputs",
            })
            assert frozen.status_code == 200, frozen.text
            started = client.post("/api/v1/scoring/tasks", json={
                "train_model": True, "materialize_scores": False, "factor_set_id": set_id,
                "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
                "window_days": 90, "validation_days": 25,
                "hyperparameters": {"factor_set_id": set_id},
                "factor_scope": {"factor_set_id": set_id}, "actor": "isolated_user",
            })
            assert started.status_code == 200, started.text
            task = client.get(f"/api/v1/scoring/tasks/{started.json()['id']}").json()
            assert active_training.get("sample_loads") == 1
            assert task["status"] == "done", task
            assert task["result"]["model"]["status"] == "validated", task
            assert task["result"]["trained_model_auto_activated"] is False
            model_id = task["result"]["model"]["model_run_id"]
            detail = client.get(f"/api/v1/scoring/models/{model_id}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["factorset_id"] == set_id
            assert detail.json()["status"] == "validated"
            assert len(detail.json()["factors"]) == 1
            assert detail.json()["factors"][0]["factor_version"] == 2
    finally:
        event.remove(engine, "before_cursor_execute", reject_expired_lease)


def _committed_training_inputs(db, tmp_path):
    """Real factor/label rows; no mocked model fit or gate implementation."""
    seed_factor_definitions(db)
    factor = db.scalar(select(Factor).where(Factor.code == "ep_ttm"))
    version = db.scalar(select(FactorVersion).where(
        FactorVersion.factor_id == factor.id, FactorVersion.version == 1,
    ))
    db.add(FactorSet(id="caller-transaction-set", name="Caller transaction set",
                     status="frozen", content_hash="a" * 64))
    db.flush()
    db.add(FactorSetMember(factor_set_id="caller-transaction-set", factor_id=factor.id,
                          factor_version_id=version.id, factor_code="ep_ttm",
                          factor_version=1, role="feature", missing_policy="exclude"))
    path = tmp_path / "caller-transaction.duckdb"
    db.add(FactorSystemConfig(id=1, feature_enabled=1, warehouse_path=str(path),
                             extra_json=json.dumps({"gates": {"ic_min": 0.0, "ic_max": 1.01,
                                                                 "min_sample_count": 0}})))
    db.commit()
    warehouse = FactorWarehouse(path)
    start = date(2025, 1, 1)
    created = datetime(2025, 1, 1)
    factors, targets = [], []
    for day in range(80):
        signal_date = start + timedelta(days=day)
        for index in range(10):
            symbol = f"{index + 1:06d}"
            value = (index - 4.5) / 3 + day * .005
            factors.append({
                "symbol": symbol, "trade_date": signal_date,
                "factor_code": "ep_ttm", "factor_version": 1,
                "raw_value": value, "winsorized_value": value, "normalized_value": value,
                "is_imputed": False, "imputation_method": None, "eligible": True,
                "data_cutoff_at": created, "calc_batch_id": "factor-train", "created_at": created,
            })
            targets.append({
                "symbol": symbol, "signal_date": signal_date,
                "entry_date": signal_date + timedelta(days=1),
                "exit_date": signal_date + timedelta(days=5),
                "target_code": "target_5d_return", "target_value": .04 * value + index * 1e-5,
                "is_tradable": True, "invalid_reason": None,
                "calc_batch_id": "target-train", "created_at": created,
            })
    warehouse.upsert_frame("factor_values", pd.DataFrame(factors))
    warehouse.upsert_frame("factor_targets", pd.DataFrame(targets))
    return warehouse, start


def _train_candidate(db, warehouse, start, run_id):
    return ridge_model.train_rolling_ridge(
        db, warehouse, factor_calc_batch_id="factor-train", target_calc_batch_id="target-train",
        factor_set_id="caller-transaction-set", data_cutoff_date=start + timedelta(days=90),
        window_days=70, validation_days=15, model_run_id=run_id,
        gate=ridge_model.ModelGate(minimum_samples=100, minimum_symbols=5,
                                   minimum_trade_dates=30, minimum_validation_dates=10,
                                   minimum_validation_ic=0.0),
    )


@pytest.mark.parametrize("flush_before_training", [False, True], ids=["pending", "already-flushed"])
def test_training_preserves_caller_uncommitted_writes(
    db_session, tmp_path, flush_before_training,
):
    warehouse, start = _committed_training_inputs(db_session, tmp_path)
    marker = Symbol(symbol="999998", name="Caller uncommitted", asset_type="stock", market="CN")
    db_session.add(marker)
    if flush_before_training:
        db_session.flush()
        # Flushed data is still caller-owned even though new/dirty are empty.
        assert not db_session.new and not db_session.dirty
    caller_transaction = db_session.get_transaction()
    result = _train_candidate(db_session, warehouse, start, "preserve-caller-training")
    assert result.status == "validated", result
    assert db_session.get_transaction() is caller_transaction
    assert db_session.scalar(select(Symbol).where(Symbol.symbol == "999998")) is marker
    with Session(bind=db_session.get_bind()) as observer:
        assert observer.scalar(select(Symbol).where(Symbol.symbol == "999998")) is None
        assert observer.get(FactorModelRun, result.model_run_id) is None
    db_session.rollback()
    assert db_session.scalar(select(Symbol).where(Symbol.symbol == "999998")) is None
    assert db_session.get(FactorModelRun, result.model_run_id) is None


def test_training_preserves_transaction_owned_by_external_connection(db_session, tmp_path):
    warehouse, start = _committed_training_inputs(db_session, tmp_path)
    engine = db_session.get_bind()
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(insert(Symbol).values(symbol="999997", name="External transaction",
                                                      asset_type="stock", market="CN"))
            with Session(bind=connection) as caller_session:
                assert not caller_session.in_transaction()
                result = _train_candidate(caller_session, warehouse, start, "preserve-external-training")
                assert result.status == "validated", result
                assert transaction.is_active
                assert connection.scalar(select(Symbol.id).where(Symbol.symbol == "999997")) is not None
                with Session(bind=engine) as observer:
                    assert observer.scalar(select(Symbol.id).where(Symbol.symbol == "999997")) is None
                    assert observer.get(FactorModelRun, result.model_run_id) is None
            assert transaction.is_active
        finally:
            transaction.rollback()
    assert db_session.scalar(select(Symbol).where(Symbol.symbol == "999997")) is None
    assert db_session.get(FactorModelRun, "preserve-external-training") is None
