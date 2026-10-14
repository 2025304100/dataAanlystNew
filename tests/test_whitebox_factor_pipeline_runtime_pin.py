"""Isolated dispatch, runtime races and atomic model-score publication."""
from datetime import date, timedelta
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, select, update

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.factor_model import FactorModelRun
from app.models.factor_runtime import FactorRuntimeState
from app.models.score import Score
from app.schemas.async_task import FactorPipelineCreate
from app.schemas.errors import FactorSevenError
from app.services.factors import pipeline_task as pipeline
from app.services.factors.bar_mirror import BarMirrorResult
from app.services.factors.data_sync import FactorInputMirrorResult
from app.services.factors.factor_engine import FactorCalculationResult
from app.services.factors.ridge_model import RidgeTrainingResult
from app.services.factors.scoring_bridge import materialize_factor_scores, materialize_factor_score_window
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import TargetCalculationResult
from tests.test_whitebox_factor_pipeline_startup import _create_task, _setup_config
from tests.test_whitebox_factor_scoring_bridge import _seed_bridge_inputs, _TRADE_DATE

pytestmark = pytest.mark.whitebox


def _runtime(db, *, model_id="active-A", mode="ridge"):
    for identity in dict.fromkeys((model_id, "active-B")):
        if db.get(FactorModelRun, identity) is None:
            db.add(FactorModelRun(id=identity, status="validated",
                                  hyperparameters_json='{"factor_set_id":"same-set"}'))
    state = db.get(FactorRuntimeState, 1)
    if state is None:
        state = FactorRuntimeState(id=1, weight_mode=mode, active_model_run_id=model_id,
                                   updated_by="isolated_test", version=1)
        db.add(state)
    else:
        state.active_model_run_id = model_id
        state.weight_mode = mode
        state.version += 1
    db.commit()
    return state


def _payload(*, model_id="active-A", mode="ridge", **kwargs):
    return FactorPipelineCreate(
        train_model=False, materialize_scores=True,
        hyperparameters_json={"expected_model_run_id": model_id, "expected_weight_mode": mode},
        **kwargs,
    )


def _stub_calculation(monkeypatch):
    day = date(2026, 1, 5)
    monkeypatch.setattr(pipeline, "_start_task_heartbeat", lambda _id: (
        threading.Event(), SimpleNamespace(join=lambda **kwargs: None)))
    monkeypatch.setattr(pipeline, "mirror_daily_bars", Mock(return_value=BarMirrorResult(batch_id="bars")))
    monkeypatch.setattr(pipeline, "mirror_factor_inputs", Mock(return_value=FactorInputMirrorResult(batch_id="inputs")))
    monkeypatch.setattr(pipeline, "calculate_stock_factors", Mock(return_value=FactorCalculationResult(
        calc_batch_id="factors", rows_written=30, eligible_rows=30, trade_date_count=1,
        symbol_count=3, coverage_by_factor={"ep_ttm": 1.0})))
    monkeypatch.setattr(pipeline, "calculate_targets", Mock(return_value=TargetCalculationResult(
        calc_batch_id="targets", rows_written=30, tradable_rows=30, invalid_rows=0,
        signal_date_count=1, symbol_count=3)))
    monkeypatch.setattr(pipeline, "_complete_calculation_dates", lambda *args, **kwargs: {
        "accepted_dates": [day], "selected_end_date": day, "skipped_dates": []})
    monkeypatch.setattr(pipeline, "_qualified_factor_dates", lambda *args, **kwargs: {
        "accepted_dates": [day], "skipped_dates": []})
    monkeypatch.setattr("app.services.factor_model_contract.assess_factor_model_readiness",
                        lambda *args, **kwargs: SimpleNamespace(ready=True))
    scorer = Mock(return_value={"status": "PUBLISHED", "materialized_count": 3, "daily_batches": []})
    monkeypatch.setattr(pipeline, "materialize_factor_score_window", scorer)
    return scorer


@pytest.mark.parametrize("model_id,mode", [("active-B", "ridge"), ("active-A", "shadow")])
def test_dispatch_rejects_stale_expected_model_or_mode(db_session, tmp_path, monkeypatch, model_id, mode):
    _setup_config(db_session, tmp_path / "dispatch.duckdb")
    _runtime(db_session)
    worker = Mock()
    monkeypatch.setattr(pipeline, "_start_worker", worker)
    with pytest.raises(FactorSevenError) as caught:
        pipeline.create_factor_pipeline_task(_payload(model_id=model_id, mode=mode))
    assert caught.value.error_code == "MODEL_RUNTIME_CHANGED"
    assert db_session.scalars(select(AsyncTaskRecord)).all() == []
    worker.assert_not_called()


def test_dispatch_pins_legacy_scoring_and_reuses_only_the_same_request(db_session, tmp_path, monkeypatch):
    _setup_config(db_session, tmp_path / "pin.duckdb")
    _runtime(db_session)
    worker = Mock()
    monkeypatch.setattr(pipeline, "_start_worker", worker)
    first = pipeline.create_factor_pipeline_task(FactorPipelineCreate(train_model=False))
    saved = FactorPipelineCreate.model_validate_json(first["payload_json"])
    assert saved.hyperparameters_json == {"expected_model_run_id": "active-A", "expected_weight_mode": "ridge"}
    second = pipeline.create_factor_pipeline_task(FactorPipelineCreate(
        train_model=False, actor="another_actor", source_hint="another_source"))
    assert second["id"] == first["id"]
    assert worker.call_count == 1


@pytest.mark.parametrize("change", [{"factor_set_id": "different-set"}, {"full_refresh": True},
                                    {"materialize_scores": False}, {"train_model": True}])
def test_single_flight_rejects_different_intent(db_session, tmp_path, monkeypatch, change):
    _setup_config(db_session, tmp_path / "busy.duckdb")
    _runtime(db_session)
    monkeypatch.setattr(pipeline, "_start_worker", Mock())
    monkeypatch.setattr(pipeline, "_require_ready_training_factor_set", lambda _payload: None)
    first = pipeline.create_factor_pipeline_task(_payload())
    values = _payload().model_dump()
    values.update(change)
    with pytest.raises(FactorSevenError) as caught:
        pipeline.create_factor_pipeline_task(FactorPipelineCreate.model_validate(values))
    assert caught.value.error_code == "FACTOR_PIPELINE_BUSY"
    assert caught.value.extras["running_task_id"] == first["id"]
    db_session.expire_all()
    assert len(db_session.scalars(select(AsyncTaskRecord)).all()) == 1


def test_same_factor_set_different_active_model_cannot_reuse_older_task(db_session, tmp_path, monkeypatch):
    _setup_config(db_session, tmp_path / "other_model.duckdb")
    _runtime(db_session)
    monkeypatch.setattr(pipeline, "_start_worker", Mock())
    first = pipeline.create_factor_pipeline_task(_payload(factor_set_id="same-set"))
    _runtime(db_session, model_id="active-B")
    with pytest.raises(FactorSevenError) as caught:
        pipeline.create_factor_pipeline_task(_payload(model_id="active-B", factor_set_id="same-set"))
    assert caught.value.error_code == "FACTOR_PIPELINE_BUSY"
    assert caught.value.extras["running_task_id"] == first["id"]


def test_worker_checks_pin_before_mirroring(db_session, tmp_path, monkeypatch):
    _setup_config(db_session, tmp_path / "worker_start.duckdb")
    _runtime(db_session, model_id="active-B")
    task = _create_task(db_session, task_id="stale-start", payload=_payload())
    scorer = _stub_calculation(monkeypatch)
    pipeline._run_factor_pipeline(task.id)
    db_session.expire_all()
    finished = db_session.get(AsyncTaskRecord, task.id)
    assert finished.status == "failed"
    assert json.loads(finished.errors_json)[0]["error_code"] == "MODEL_RUNTIME_CHANGED"
    pipeline.mirror_daily_bars.assert_not_called()
    scorer.assert_not_called()


def test_model_switch_after_calculation_cannot_publish_other_model(db_session, tmp_path, monkeypatch):
    _setup_config(db_session, tmp_path / "race.duckdb")
    _runtime(db_session)
    task = _create_task(db_session, task_id="stale-after-calculation", payload=_payload())
    scorer = _stub_calculation(monkeypatch)
    original_target = pipeline.calculate_targets

    def switch_after_calculation(*args, **kwargs):
        with get_session_local()() as other:
            state = other.get(FactorRuntimeState, 1)
            state.active_model_run_id = "active-B"
            state.version += 1
            other.commit()
        return original_target(*args, **kwargs)

    monkeypatch.setattr(pipeline, "calculate_targets", switch_after_calculation)
    pipeline._run_factor_pipeline(task.id)
    db_session.expire_all()
    finished = db_session.get(AsyncTaskRecord, task.id)
    assert finished.status == "failed"
    assert json.loads(finished.errors_json)[0]["error_code"] == "MODEL_RUNTIME_CHANGED"
    assert db_session.get(FactorRuntimeState, 1).active_model_run_id == "active-B"
    scorer.assert_not_called()


@pytest.mark.parametrize("materialize", [True, False])
def test_training_with_an_old_active_model_never_scores_or_activates_it(db_session, tmp_path, monkeypatch, materialize):
    _setup_config(db_session, tmp_path / "old_active.duckdb")
    _runtime(db_session)
    task = _create_task(db_session, task_id="train-new", payload=FactorPipelineCreate(
        train_model=True, materialize_scores=materialize))
    scorer = _stub_calculation(monkeypatch)
    monkeypatch.setattr(pipeline, "train_rolling_ridge", lambda *args, **kwargs: RidgeTrainingResult(
        model_run_id="candidate-new", status="validated", selected_alpha=1.0,
        sample_count=800, symbol_count=30, trade_date_count=100, validation_ic=0.05))
    pipeline._run_factor_pipeline(task.id)
    db_session.expire_all()
    finished = db_session.get(AsyncTaskRecord, task.id)
    assert finished.status == "done"
    result = json.loads(finished.result_json)
    assert result["completion_status"] == "AWAITING_MODEL_ACTIVATION"
    assert result["trained_model_auto_activated"] is False
    if materialize:
        assert result["scoring_bridge"]["reason"] == "TRAINED_MODEL_REQUIRES_EXPLICIT_ACTIVATION"
        assert result["scoring_bridge"]["model_run_id"] == "candidate-new"
    assert db_session.get(FactorRuntimeState, 1).active_model_run_id == "active-A"
    scorer.assert_not_called()


def test_runtime_guard_refreshes_an_already_cached_state(db_session):
    state = _runtime(db_session)
    assert state.active_model_run_id == "active-A"
    with get_session_local()() as other:
        other.execute(update(FactorRuntimeState).where(FactorRuntimeState.id == 1)
                      .values(active_model_run_id="active-B", version=2))
        other.commit()
    # The first session still holds the old ORM object. The guard must read
    # the database state, rather than its identity map.
    with pytest.raises(FactorSevenError) as caught:
        pipeline._validate_requested_runtime(db_session, _payload())
    assert caught.value.error_code == "MODEL_RUNTIME_CHANGED"
    assert caught.value.extras["actual_model_run_id"] == "active-B"


def test_runtime_guard_does_not_reuse_a_deleted_cached_state(db_session):
    _runtime(db_session)
    with get_session_local()() as other:
        other.execute(delete(FactorRuntimeState).where(FactorRuntimeState.id == 1))
        other.commit()
    with pytest.raises(FactorSevenError) as caught:
        pipeline._validate_requested_runtime(db_session, _payload())
    assert caught.value.error_code == "MODEL_RUNTIME_CHANGED"
    assert caught.value.extras["actual_model_run_id"] is None


def test_matching_runtime_pin_publishes_real_model_scores(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "matching-publication.duckdb")
    _, _, model = _seed_bridge_inputs(db_session, warehouse)
    _runtime(db_session, model_id=model.id)
    payload = _payload(model_id=model.id)
    result = materialize_factor_score_window(
        db_session, warehouse, trade_dates=[_TRADE_DATE], factor_calc_batch_id="factor-score-test",
        model_run_id=model.id, mode="ridge",
        publication_guard=lambda: pipeline._validate_requested_runtime(db_session, payload))
    db_session.commit()
    assert result["status"] == "PUBLISHED"
    assert result["materialized_count"] == 3
    scores = db_session.scalars(select(Score).where(Score.weight_mode == "ridge")).all()
    assert {score.factor_model_run_id for score in scores} == {model.id}
    assert min(score.model_alpha_score for score in scores) < max(score.model_alpha_score for score in scores)


def test_publication_guard_rolls_back_new_real_scores_preserving_history(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "atomic-publication.duckdb")
    _, _, model = _seed_bridge_inputs(db_session, warehouse)
    _runtime(db_session, model_id=model.id)
    first = materialize_factor_scores(db_session, warehouse, trade_date=_TRADE_DATE,
                                     factor_calc_batch_id="factor-score-test", model_run_id=model.id, mode="ridge")
    db_session.commit()
    original = {row.id: (row.published_at, row.model_alpha_score) for row in db_session.scalars(
        select(Score).where(Score.calc_batch_id == first.calc_batch_id)).all()}
    second_day = _TRADE_DATE + timedelta(days=1)
    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute("SELECT * FROM factor_values WHERE calc_batch_id='factor-score-test'").fetchdf()
    rows["trade_date"] = second_day
    warehouse.upsert_frame("factor_values", rows)
    payload = _payload(model_id=model.id)
    checks = 0

    def publication_guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            # Inject the stale identity exactly after real score/lineage
            # writes, while they are still inside the publication savepoint.
            db_session.execute(update(FactorRuntimeState).where(FactorRuntimeState.id == 1)
                               .values(active_model_run_id="active-B", version=2),
                               execution_options={"synchronize_session": False})
        pipeline._validate_requested_runtime(db_session, payload)

    with pytest.raises(FactorSevenError) as caught:
        materialize_factor_score_window(
            db_session, warehouse, trade_dates=[second_day], factor_calc_batch_id="factor-score-test",
            model_run_id=model.id, mode="ridge", publication_guard=publication_guard)
    assert caught.value.error_code == "MODEL_RUNTIME_CHANGED"
    assert checks == 2
    db_session.commit()
    assert db_session.scalars(select(Score).where(Score.trade_date == second_day)).all() == []
    preserved = {row.id: (row.published_at, row.model_alpha_score) for row in db_session.scalars(
        select(Score).where(Score.calc_batch_id == first.calc_batch_id)).all()}
    assert preserved == original
