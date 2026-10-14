"""A completed generation can resume without replay or a changed random stream."""
from __future__ import annotations

import json

import pytest

from app.services.factors.mining.genetic_algorithm import GAConfig, run_ga_loop

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("mode,adaptive", [("naive", False), ("advanced", True)])
def test_generation_checkpoint_reproduces_uninterrupted_evolution(mode, adaptive):
    cfg = GAConfig(population_size=8, max_generations=5, seed=0,
                   convergence_threshold=-1, convergence_generations=20,
                   selection_mode=mode, adaptive=adaptive,
                   selected_fields=("close", "volume"))
    population = [{"id": str(i), "formula": f"mean(close,{5 + i})",
                   "category": "trend", "formula_hash": str(i)} for i in range(8)]

    def evaluate(item):
        return {"icir": len(item["formula"]) / 100,
                "coverage": 1.0, "turnover": 0.1, "complexity": 2}

    whole = run_ga_loop(cfg, population, evaluate=evaluate)
    checkpoints = []

    def save(state):
        checkpoints.append(json.loads(json.dumps(state, allow_nan=False)))

    partial = run_ga_loop(cfg, population, evaluate=evaluate,
                          on_checkpoint=save, should_stop=lambda: len(checkpoints) >= 2)
    assert partial.generations_run == 2
    assert checkpoints[-1]["next_generation"] == 2
    resumed = run_ga_loop(cfg, population, evaluate=evaluate,
                         initial_state=checkpoints[-1])
    assert resumed.generations_run == whole.generations_run
    assert resumed.total_trials == whole.total_trials == 40
    assert resumed.best_icir == whole.best_icir
    assert resumed.best_formula == whole.best_formula
    assert resumed.history == whole.history[2:]


def test_mutated_candidates_are_evaluated_and_identified_as_their_actual_formula():
    cfg = GAConfig(population_size=8, max_generations=2, seed=0,
                   convergence_threshold=-1, convergence_generations=20)
    population = [{"id": str(i), "formula": f"mean(close,{5 + i})",
                   "canonical_formula": f"mean(close,{5 + i})",
                   "formula_hash": f"parent-{i}", "category": "trend"} for i in range(8)]
    observed = []

    def evaluate(candidate):
        observed.append(dict(candidate))
        return {"icir": 0.1, "coverage": 1.0, "turnover": 0.1, "complexity": 2}

    run_ga_loop(cfg, population, evaluate=evaluate)
    children = [candidate for candidate in observed[8:]
                if candidate["operation"] in ("mutation", "crossover")]
    assert children
    assert all(candidate["canonical_formula"] == candidate["formula"] for candidate in children)
    changed = [candidate for candidate in children if candidate["formula"] not in
               {parent["formula"] for parent in population}]
    assert changed
    assert not any(candidate["formula_hash"].startswith("parent-") for candidate in changed)
