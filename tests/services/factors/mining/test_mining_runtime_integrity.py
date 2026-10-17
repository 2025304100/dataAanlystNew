"""Public HTTP → real worker → temporary DuckDB → real final evaluation; no LLM calls."""
from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from test_m1_closure import mining_env, warehouse_path
from test_factor_mining_routes import _client
from app.models.mining_candidate_pool import TrainingCandidatePool, TrainingCandidatePoolSnapshot

pytestmark = pytest.mark.whitebox


def _seed(db):
    members = [{"symbol": f"{i:06d}", "symbol_id": i + 1} for i in range(12)]
    db.add(TrainingCandidatePool(id="runtime-pool", name="隔离回归", source_type="filter",
                                status="frozen", member_count=len(members)))
    db.add(TrainingCandidatePoolSnapshot(
        id="runtime-snapshot", pool_id="runtime-pool", members_json=json.dumps(members),
        rule_hash="runtime-rule", data_cutoff_at=datetime(2027, 3, 1),
        is_locked=1, member_count=len(members)))
    db.commit()


def _inputs(path, *, generations=1, finalize=2):
    return {
        "candidate_pool_snapshot_id": "runtime-snapshot",
        "data_cutoff_at": "2027-03-01T00:00:00",
        "start_date": "2026-01-05T00:00:00", "end_date": "2027-02-01T00:00:00",
        "rebalance_frequency": "daily", "target_horizon": 5,
        "train_ratio": 0.55, "validation_ratio": 0.25, "random_seed": 0,
        "finalize_top_k": finalize,
        "evolution_params": {"population_size": 6, "max_generations": generations,
                             "convergence_threshold": -1, "convergence_generations": 30},
        "filter_config": {"selected_fields": ["close", "open", "high", "low", "volume"],
                          "warehouse_path": path},
    }


def _wait(client, db, run_id, predicate, *, timeout=40):
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        db.expire_all()
        last = client.get(f"/factor-mining/runs/{run_id}").json()
        if predicate(last):
            return last
        time.sleep(0.05)
    pytest.fail(f"Mining run did not reach expected state: {last}")


def test_empty_warehouse_is_rejected_before_worker_or_batch_creation(mining_env, tmp_path):
    db = mining_env
    _seed(db)
    client = _client(db)
    created = client.post("/factor-mining/runs", json=_inputs(str(tmp_path / "empty.duckdb")))
    assert created.status_code == 422, created.text
    assert created.json()["detail"]["error_code"] == "MINING_PREFLIGHT_BUDGET_BLOCKED"
    assert client.get("/factor-mining/runs").json()["total"] == 0
    assert client.get("/factor-mining/locks/status").json()["miningDomain"]["busy"] is False


def test_public_worker_produces_real_final_evaluation(mining_env, warehouse_path):
    db = mining_env
    _seed(db)
    client = _client(db)
    created = client.post("/factor-mining/runs", json=_inputs(warehouse_path))
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    detail = _wait(client, db, run_id, lambda run: run["status"] in ("succeeded", "failed"))
    assert detail["status"] == "succeeded", detail
    candidates = client.get(f"/factor-mining/runs/{run_id}/candidates").json()["items"]
    evaluated = [candidate for candidate in candidates if candidate.get("latest_evaluation_id")]
    assert len(evaluated) == 2
    assert all(candidate["latest_ic"] is not None for candidate in evaluated)
    evaluation = client.get(f"/factor-mining/evaluations/{evaluated[0]['latest_evaluation_id']}").json()
    assert evaluation["factor_version_id"] == evaluated[0]["factor_version_id"]
    assert evaluation["metrics"]
    assert evaluation["metrics"]["evaluation_segment"] == "test"
    assert evaluation["metrics"]["evaluation_start"] == evaluation["metrics"]["time_split"]["test_start"]


def test_pause_then_resume_retains_completed_generations_and_original_inputs(mining_env, warehouse_path):
    db = mining_env
    _seed(db)
    client = _client(db)
    created = client.post("/factor-mining/runs", json=_inputs(warehouse_path, generations=12, finalize=0))
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    before = _wait(client, db, run_id, lambda run: run["current_generation"] >= 1)
    paused = client.post(f"/factor-mining/runs/{run_id}/pause")
    assert paused.status_code == 200, paused.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        db.expire_all()
        if not client.get("/factor-mining/locks/status").json()["miningDomain"]["busy"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("paused worker did not release its lock")
    resumed = client.post(f"/factor-mining/runs/{run_id}/resume")
    assert resumed.status_code == 200, resumed.text
    after = _wait(client, db, run_id, lambda run: run["status"] in ("succeeded", "failed"))
    assert after["status"] == "succeeded", after
    assert after["current_generation"] == 11
    assert after["total_trials"] == 72
    assert after["total_trials"] > before["total_trials"]
    assert after["random_seed"] == 0
    assert after["evolution_params"]["population_size"] == 6
    assert after["split_config"] == {"train_ratio": 0.55, "validation_ratio": 0.25}


def test_early_stop_releases_worker_and_does_not_execute_final_test(mining_env, warehouse_path):
    db = mining_env
    _seed(db)
    client = _client(db)
    created = client.post("/factor-mining/runs", json=_inputs(warehouse_path, generations=12, finalize=2))
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    _wait(client, db, run_id, lambda run: run["current_generation"] >= 1)
    response = client.post(f"/factor-mining/runs/{run_id}/stop")
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        db.expire_all()
        if not client.get("/factor-mining/locks/status").json()["miningDomain"]["busy"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("stopped worker did not release its lock")
    detail = client.get(f"/factor-mining/runs/{run_id}").json()
    assert detail["status"] == "converged"
    assert detail["current_generation"] < 11
    candidates = client.get(f"/factor-mining/runs/{run_id}/candidates").json()["items"]
    assert candidates
    assert not any(candidate["latest_evaluation_id"] for candidate in candidates)
