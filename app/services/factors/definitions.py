"""Stable factor definitions for the first local factor batch."""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class FactorDefinition:
    code: str
    name: str
    category: str
    direction: str
    formula: str
    version: int = 1
    missing_policy: str = "exclude"


FACTOR_DEFINITIONS: tuple[FactorDefinition, ...] = (
    FactorDefinition(
        code="ep_ttm",
        name="E/P (TTM)",
        category="fundamental",
        direction="higher_better",
        formula="1 / pe_ttm, pe_ttm > 0",
    ),
    FactorDefinition(
        code="negative_pb",
        name="Negative PB",
        category="fundamental",
        direction="higher_better",
        formula="-pb, pb > 0",
    ),
    FactorDefinition(
        code="main_inflow_5d_ratio",
        name="5-day Main Inflow / Turnover",
        category="capital_flow",
        direction="higher_better",
        formula="sum(main_net_inflow, 5d) / sum(amount, 5d)",
    ),
    FactorDefinition(
        code="turnover_z20",
        name="20-day Turnover Z-Score",
        category="sentiment",
        direction="nonlinear",
        formula="(turnover - mean_20d) / stddev_pop_20d",
    ),
)

FACTOR_BY_CODE = {definition.code: definition for definition in FACTOR_DEFINITIONS}

_SOURCE_MAPPINGS = {
    "ep_ttm": {"table": "raw_valuation_snapshots", "field": "pe_ttm"},
    "negative_pb": {"table": "raw_valuation_snapshots", "field": "pb"},
    "main_inflow_5d_ratio": {
        "tables": ["raw_fund_flows", "raw_daily_bars"],
        "fields": ["main_net_inflow", "amount"],
    },
    "turnover_z20": {
        "table": "raw_daily_bars",
        "field": "turnover_rate",
    },
}

_PARAMS = {
    "ep_ttm": {"valuation_max_age_days": 7, "positive_pe_only": True},
    "negative_pb": {"valuation_max_age_days": 7, "positive_pb_only": True},
    "main_inflow_5d_ratio": {"window": 5, "minimum_periods": 5},
    "turnover_z20": {"window": 20, "minimum_periods": 20},
}


def seed_factor_definitions(db: Session) -> int:
    """Idempotently seed system factors and their immutable first versions."""
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion

    created = 0
    for definition in FACTOR_DEFINITIONS:
        factor = db.execute(
            select(Factor).where(Factor.code == definition.code)
        ).scalars().first()
        if factor is None:
            factor = Factor(
                code=definition.code,
                name=definition.name,
                category=definition.category,
                direction=definition.direction,
                status="active",
                source_type="local_akshare",
                frequency="daily",
                default_missing_policy=definition.missing_policy,
                is_active=1,
                description=definition.name,
                formula_expr=definition.formula,
            )
            db.add(factor)
            db.flush()
            created += 1
        version = db.execute(
            select(FactorVersion).where(
                FactorVersion.factor_id == factor.id,
                FactorVersion.version == definition.version,
            )
        ).scalars().first()
        if version is None:
            db.add(
                FactorVersion(
                    factor_id=factor.id,
                    version=definition.version,
                    formula_expr=definition.formula,
                    params_json=json.dumps(
                        _PARAMS[definition.code], sort_keys=True
                    ),
                    direction=definition.direction,
                    source_mapping_json=json.dumps(
                        _SOURCE_MAPPINGS[definition.code], sort_keys=True
                    ),
                    change_note="Initial local AkShare factor definition",
                    is_latest=1,
                )
            )
            created += 1
    db.flush()
    return created
