"""Zero is a real no-cross-track policy through the public mining HTTP lifecycle."""
from __future__ import annotations

import json
import time

import pytest

from test_m1_closure import mining_env, warehouse_path
from test_factor_mining_routes import _client
from test_mining_runtime_integrity import _inputs, _seed, _wait

pytestmark = pytest.mark.whitebox
_UNSET = object()


def _evolve(client, db, warehouse_path, *, ratio=_UNSET, explicit_mode=True, population=20, generations=3):
    payload = _inputs(warehouse_path, generations=generations, finalize=0)
    payload["evolution_params"].update({
        "population_size": population, "selection_ratio": 0.05, "adaptive": False, "ai_enabled": False,
    })
    if ratio is not _UNSET:
        payload["evolution_params"]["cross_category_ratio"] = ratio
    if explicit_mode:
        payload["evolution_params"]["selection_mode"] = "advanced"
    created = client.post("/factor-mining/runs", json=payload)
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    done = _wait(client, db, run_id, lambda run: run["status"] in ("succeeded", "failed"))
    assert done["status"] == "succeeded", done
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        db.expire_all()
        if not client.get("/factor-mining/locks/status").json()["miningDomain"]["busy"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("completed worker did not release its lock")
    generations = client.get(f"/factor-mining/runs/{run_id}/generations").json()
    candidates = client.get(f"/factor-mining/runs/{run_id}/candidates?page_size=200").json()["items"]
    return generations, {candidate["canonical_formula"] for candidate in candidates}


@pytest.mark.parametrize("explicit_mode", [True, False])
def test_zero_cross_track_policy_evolves_like_an_effectively_zero_probability(mining_env, warehouse_path, explicit_mode):
    db = mining_env
    _seed(db)
    client = _client(db)
    # With this fixed seed and finite crossover count, 1e-12 makes no cross-track draws.
    near_zero_generations, near_zero_formulas = _evolve(client, db, warehouse_path, ratio=1e-12,
                                                       explicit_mode=explicit_mode)
    zero_generations, zero_formulas = _evolve(client, db, warehouse_path, ratio=0.0,
                                             explicit_mode=explicit_mode)
    assert any(generation["crossover_count"] > 0 for generation in near_zero_generations)
    assert any(len(json.loads(generation["category_distribution_json"])) > 1
               for generation in near_zero_generations)
    assert zero_formulas == near_zero_formulas
    assert [generation["cross_category_ratio"] for generation in zero_generations] == [0.0] * len(zero_generations)
    assert [generation["population_size"] for generation in zero_generations] == [
        generation["population_size"] for generation in near_zero_generations]


@pytest.mark.parametrize("ratio_args", [{}, {"ratio": None}], ids=["omitted", "null"])
def test_omitted_or_null_cross_track_policy_keeps_the_default(mining_env, warehouse_path, ratio_args):
    db = mining_env
    _seed(db)
    generations, _ = _evolve(_client(db), db, warehouse_path, population=6, generations=1, **ratio_args)
    assert len(generations) == 1
    assert generations[0]["cross_category_ratio"] == 0.2
