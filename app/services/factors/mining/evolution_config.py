"""Shared validation for the fixed reproduction rates accepted by HTTP and workers."""
from __future__ import annotations

from typing import Any, Mapping

from app.services.factors.mining.genetic_algorithm import GAConfig


def validated_evolution_rates(evolution: Mapping[str, Any] | None) -> dict[str, float]:
    """Keep explicit values and apply the engine's existing bounds and sum rule."""
    config = evolution or {}
    defaults = {'mutation_rate': .55, 'crossover_rate': .25, 'random_rate': .20}
    values: dict[str, float] = {}
    for name, default in defaults.items():
        value = config.get(name)
        try:
            values[name] = default if value is None else float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{name} 必须是有效数值。') from exc
    GAConfig(**values)
    return values
