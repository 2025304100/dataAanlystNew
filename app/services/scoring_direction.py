"""Versioned direction contract for already normalized 0..100 scores."""
from __future__ import annotations

from copy import deepcopy


DIRECTION_CONTRACT = "standard_score_v1"


class ScoringConfigValidationError(ValueError):
    pass


def prepare_scoring_config(config: dict, *, legacy: bool = False) -> dict:
    if not isinstance(config, dict):
        raise ScoringConfigValidationError("Scoring config must be an object")
    result = deepcopy(config)
    contract = result.get("direction_contract")
    if contract not in (None, DIRECTION_CONTRACT):
        raise ScoringConfigValidationError("Unsupported scoring direction contract")
    old_metadata = legacy and contract is None
    dimensions = result.get("dimensions", [])
    if not isinstance(dimensions, list):
        raise ScoringConfigValidationError("Scoring dimensions must be a list")
    for dim in dimensions:
        if not isinstance(dim, dict) or not dim.get("key"):
            raise ScoringConfigValidationError("Each scoring dimension requires a key")
        factors = dim.get("factors", [])
        if not isinstance(factors, list):
            raise ScoringConfigValidationError("Scoring factors must be a list")
        for factor in factors:
            if not isinstance(factor, dict) or not factor.get("key"):
                raise ScoringConfigValidationError("Each scoring factor requires a key")
            # Before this contract, directions were display-only metadata and
            # every score was already normalized. Preserve those old results.
            direction = "higher_better" if old_metadata else factor.get("direction", "higher_better")
            if direction not in {"higher_better", "lower_better"}:
                raise ScoringConfigValidationError("Range scoring directions are not supported; choose higher_better or lower_better for the standard score")
            factor["direction"] = direction
    result["direction_contract"] = DIRECTION_CONTRACT
    return result
