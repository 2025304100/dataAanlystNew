"""Pure current-execution checks; never rewrite historical validation evidence."""
from __future__ import annotations

import json
from typing import Any

from app.services.factors.factor_compiler import FactorCompiler


def native_factor_contract_matches(code: str | None, *, formula: str, params: dict,
        source_mapping: dict, direction: str, postprocess: dict | None,
        valuation_max_age_days: int = 7) -> bool:
    """Exactly mirror the native executor contract, never a name-only exemption."""
    from app.services.factors.definitions import FACTOR_BY_CODE, _PARAMS, _SOURCE_MAPPINGS

    definition = FACTOR_BY_CODE.get(code)
    return bool(definition
        and "".join(formula.split()) == "".join(definition.formula.split())
        and params == _PARAMS[code]
        and source_mapping == _SOURCE_MAPPINGS[code]
        and direction == definition.direction
        and postprocess is None
        and (code not in ("ep_ttm", "negative_pb")
             or valuation_max_age_days == params.get("valuation_max_age_days")))


def frozen_postprocess_supported(postprocess: dict | None) -> bool:
    """Capabilities of the frozen-set calculation path, beyond DSL syntax."""
    if not postprocess:
        return True
    if not isinstance(postprocess, dict):
        return False
    rank = postprocess.get("rank") or {}
    return bool(isinstance(rank, dict)
        and not postprocess.get("neutralize")
        and not (postprocess.get("rank") and postprocess.get("zscore"))
        and rank.get("method", "percentile") == "percentile")


def frozen_source_mapping_supported(source: dict, dependencies: dict) -> bool:
    """Mappings may only reference fields and tables used by the current plan."""
    try:
        fields = source.get("fields", []) + ([source["field"]] if source.get("field") else [])
        tables = source.get("tables", []) + ([source["table"]] if source.get("table") else [])
        return (set(fields).issubset(dependencies.get("fields", []))
                and set(tables).issubset(dependencies.get("source_tables", [])))
    except (TypeError, AttributeError):
        return False


def can_execute_version(version: Any, *, factor_code: str | None = None) -> bool:
    """Old compiler labels are acceptable when the current strict DSL accepts them."""
    try:
        params = json.loads(getattr(version, "params_json", None) or "{}")
        postprocess = json.loads(getattr(version, "postprocess_json", None) or "null")
        source = json.loads(getattr(version, "source_mapping_json", None) or "{}")
        if (not isinstance(params, dict) or not isinstance(source, dict)
                or postprocess is not None and not isinstance(postprocess, dict)):
            return False
        if native_factor_contract_matches(factor_code, formula=version.formula_expr,
                params=params, source_mapping=source, direction=version.direction,
                postprocess=postprocess):
            return True
        if not frozen_postprocess_supported(postprocess):
            return False
        result = FactorCompiler().compile(
            formula=getattr(version, "formula_expr", None), params=params,
            direction=getattr(version, "direction", None) or "higher_better",
            postprocess=postprocess, strict_fields=True,
        )
        return bool(result.is_valid and result.execution_plan is not None
                    and frozen_source_mapping_supported(source, result.execution_plan.data_dependencies))
    except (TypeError, ValueError, AttributeError):
        return False
