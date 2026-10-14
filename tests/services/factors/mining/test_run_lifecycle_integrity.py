"""Public mining API regressions for reproducibility and truthful lifecycle state."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from test_factor_mining_routes import _client, _seed_run, _seed_snapshot

pytestmark = pytest.mark.whitebox


@dataclass
class Submitted:
    task_id: str = "isolated-task"
    queue_position: int = 0


def _request(snapshot="snapshot-integrity"):
    return {
        "candidate_pool_snapshot_id": snapshot,
        "data_cutoff_at": "2026-10-14T00:00:00",
        "start_date": "2025-01-01T00:00:00",
        "end_date": "2026-09-30T00:00:00",
        "rebalance_frequency": "daily",
        "target_horizon": 5,
        "train_ratio": 0.55,
        "validation_ratio": 0.25,
        "random_seed": 0,
        "finalize_top_k": 3,
        "evolution_params": {"population_size": 8, "max_generations": 4},
        "filter_config": {"selected_fields": ["close", "volume"],
                          "warehouse_path": "isolated.duckdb"},
    }


def test_created_batch_detail_retains_original_experiment_configuration(db_session, monkeypatch):
    from app.api.routes.factor_mining import mining_runner

    _seed_snapshot(db_session, "snapshot-integrity")
    monkeypatch.setattr(mining_runner, "submit_mining_run", lambda *a, **kw: Submitted())
    client = _client(db_session)
    created = client.post("/factor-mining/runs", json=_request())
    assert created.status_code == 201, created.text
    detail = client.get(f"/factor-mining/runs/{created.json()['run_id']}").json()
    assert detail["evolution_params"]["population_size"] == 8
    assert detail["selected_fields"] == ["close", "volume"]
    assert detail["split_config"] == {"train_ratio": 0.55, "validation_ratio": 0.25}
    assert detail["random_seed"] == 0
    assert detail["candidate_pool_id"] == "pool-snapshot-integrity"


def test_legacy_batch_without_checkpoint_is_not_silently_restarted(db_session, monkeypatch):
    from app.api.routes.factor_mining import mining_runner
    _seed_run(db_session, "legacy-paused", status="paused")
    _seed_snapshot(db_session, "snap-c1")
    submitted = []
    monkeypatch.setattr(mining_runner, "submit_mining_run", lambda *a, **kw: submitted.append(a))
    response = _client(db_session).post("/factor-mining/runs/legacy-paused/resume")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "MINING_CHECKPOINT_UNAVAILABLE"
    assert submitted == []


@pytest.mark.parametrize("action", ["cancel", "pause", "stop", "discard"])
def test_cancelled_queued_batch_releases_mining_domain_without_touching_other_writer(db_session, action):
    from app.services.factors.mining.task_lock import acquire_write_slot
    _seed_snapshot(db_session, "snapshot-integrity")
    acquire_write_slot(db_session, task_id="other-writer", run_id="other-run")
    client = _client(db_session)
    created = client.post("/factor-mining/runs", json=_request())
    assert created.status_code == 201, created.text
    assert created.json()["queue_position"] == 1
    run_id = created.json()["run_id"]
    assert client.get(f"/factor-mining/runs/{run_id}").json()["status"] == "queued"
    response = client.post(f"/factor-mining/runs/{run_id}/{action}")
    assert response.status_code == 200, response.text
    db_session.expire_all()
    locks = client.get("/factor-mining/locks/status").json()
    assert locks["miningDomain"]["busy"] is False
    assert locks["duckdbWrite"]["taskId"] == "other-writer"
    assert created.json()["task_id"] not in locks["duckdbWrite"]["queue"]


@pytest.mark.parametrize("overrides", [
    {"start_date": "2026-10-01T00:00:00", "end_date": "2026-09-01T00:00:00"},
    {"end_date": "2026-10-15T00:00:00"},
    {"train_ratio": 0.75, "validation_ratio": 0.3},
])
def test_invalid_window_is_rejected_before_starting_a_batch(db_session, monkeypatch, overrides):
    from app.api.routes.factor_mining import mining_runner

    _seed_snapshot(db_session, "snapshot-integrity")
    submitted = []
    monkeypatch.setattr(mining_runner, "submit_mining_run", lambda *a, **kw: submitted.append(a))
    client = _client(db_session)
    response = client.post("/factor-mining/runs", json={**_request(), **overrides})
    assert response.status_code == 422, response.text
    assert submitted == []
    assert client.get("/factor-mining/runs").json()["total"] == 0


@pytest.mark.parametrize("action", ["pause", "cancel", "resume", "stop", "discard"])
def test_completed_batch_rejects_state_mutation_with_a_readable_conflict(db_session, monkeypatch, action):
    from app.api.routes.factor_mining import mining_runner

    _seed_run(db_session, "finished", status="succeeded")
    submitted = []
    monkeypatch.setattr(mining_runner, "submit_mining_run", lambda *a, **kw: submitted.append(a))
    client = _client(db_session)
    response = client.post(f"/factor-mining/runs/finished/{action}")
    assert response.status_code == 409, response.text
    assert submitted == []
    assert client.get("/factor-mining/runs/finished").json()["status"] == "succeeded"
