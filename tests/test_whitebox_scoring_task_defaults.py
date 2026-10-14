"""Public task defaults must reach a real pipeline request rather than None/skip."""
from types import SimpleNamespace

import pytest

from app.services.factors import __facade__ as facade
from app.services.factors import config, pipeline_task
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("train,scores,mirror", [
    (False, False, True), (False, True, False), (True, False, False), (True, True, False),
])
def test_optional_windows_use_schema_defaults_without_suppressing_training(monkeypatch, train, scores, mirror):
    received = []
    monkeypatch.setattr(pipeline_task, "create_factor_pipeline_task", lambda payload: received.append(payload) or {"id": "real-task"})
    result = facade.create_scoring_task(train_model=train, materialize_scores=scores,
                                       factor_set_id="frozen-set" if train else None)
    assert result["id"] == "real-task"
    payload = received[0]
    assert (payload.window_days, payload.validation_days) == (250, 50)
    assert payload.train_model == train and payload.materialize_scores == scores
    assert payload.mirror_only == mirror


def test_pinned_activation_scoring_keeps_model_expectation_with_default_windows(monkeypatch):
    received = []
    monkeypatch.setattr(pipeline_task, "create_factor_pipeline_task", lambda payload: received.append(payload) or {"id": "score-task"})
    facade.create_scoring_task(train_model=False, materialize_scores=True, factor_set_id="frozen-set",
        hyperparameters={"factor_set_id": "frozen-set", "expected_model_run_id": "activated-model", "expected_weight_mode": "ridge"},
        factor_scope={"factor_set_id": "frozen-set", "asset_type": "stock"})
    assert received[0].hyperparameters_json["expected_model_run_id"] == "activated-model"
    assert received[0].hyperparameters_json["expected_weight_mode"] == "ridge"
    assert received[0].mirror_only is False


@pytest.mark.parametrize("train", [True, False])
def test_public_api_training_and_activation_requests_reach_correct_pipeline_mode(monkeypatch, train):
    from app.api.routes.scoring_facade import CreateScoringTaskRequest, api_create_scoring_task
    received = []
    monkeypatch.setattr(pipeline_task, "create_factor_pipeline_task", lambda payload: received.append(payload) or {"id": "api-task"})
    request = CreateScoringTaskRequest(train_model=train, materialize_scores=not train, factor_set_id="frozen-set",
        hyperparameters={"factor_set_id": "frozen-set"} if train else {
            "factor_set_id": "frozen-set", "expected_model_run_id": "activated-model", "expected_weight_mode": "ridge"},
        factor_scope={"factor_set_id": "frozen-set", "asset_type": "stock"})
    assert api_create_scoring_task(request)["id"] == "api-task"
    assert received[0].mirror_only is False
    assert (received[0].window_days, received[0].validation_days) == (250, 50)
    assert received[0].train_model is train and received[0].materialize_scores is not train


def test_explicit_mirror_only_training_is_rejected_before_task_creation(monkeypatch):
    monkeypatch.setattr(pipeline_task, "create_factor_pipeline_task", lambda payload: pytest.fail("training was silently skipped"))
    with pytest.raises(ValueError, match="mirror_only"):
        facade.create_scoring_task(train_model=True, materialize_scores=False, mirror_only=True, factor_set_id="frozen-set")


def test_public_warehouse_initialization_uses_actual_store(tmp_path, monkeypatch):
    path = tmp_path / "public-initialized.duckdb"
    sentinel = SimpleNamespace(warehouse_available=False)
    monkeypatch.setattr(config, "get_current_factor_system_config", lambda: SimpleNamespace(warehouse_path=str(path)))
    monkeypatch.setattr(facade, "get_score_runtime_overview", lambda: sentinel)
    assert facade.init_scoring_warehouse(force=True) is sentinel
    with FactorWarehouse(path).connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM factor_values").fetchone()[0] == 0
