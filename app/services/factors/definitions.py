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
    frequency: str = "daily"
    model_enabled: bool = True
    health_required: bool = True


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
        code="roe_yoy_growth",
        name="ROE YoY Growth (ppt)",
        category="fundamental",
        direction="higher_better",
        formula="roe_ttm - ref(roe_ttm, 250)",
        frequency="quarterly",
    ),
    FactorDefinition(
        code="main_inflow_5d_ratio",
        name="5-day Main Inflow / Turnover",
        category="capital_flow",
        direction="higher_better",
        formula="sum(main_net_inflow, 5) / sum(amount, 5)",
    ),
    FactorDefinition(
        code="lhb_institution_net_ratio",
        name="LHB Institution Net / Turnover",
        category="capital_flow",
        direction="higher_better",
        formula="lhb_institution_net / amount",
        missing_policy="exclude",
        model_enabled=False,
        health_required=False,
    ),
    FactorDefinition(
        code="turnover_z20",
        name="20-day Turnover Z-Score",
        category="sentiment",
        direction="nonlinear",
        formula="(turnover - mean(turnover, 20)) / stddev(turnover, 20)",
    ),
    FactorDefinition(
        code="hot_rank_attention",
        name="EastMoney Hot-Rank Attention",
        category="sentiment",
        direction="nonlinear",
        formula="1 - hot_rank_pct / 100",
        missing_policy="exclude",
        model_enabled=False,
        health_required=False,
    ),
    FactorDefinition(
        code="tail_accumulation_proxy",
        name="Tail-session Accumulation Proxy",
        category="capital_flow",
        direction="higher_better",
        formula=(
            "log(tail_activity_ratio) "
            "+ 20*tail_return + close_location - 0.5"
        ),
        missing_policy="exclude",
        model_enabled=False,
        health_required=False,
    ),
)

FACTOR_BY_CODE = {definition.code: definition for definition in FACTOR_DEFINITIONS}

_SOURCE_MAPPINGS = {
    "ep_ttm": {"table": "raw_valuation_snapshots", "field": "pe_ttm"},
    "negative_pb": {"table": "raw_valuation_snapshots", "field": "pb"},
    "roe_yoy_growth": {
        "table": "raw_financial_reports",
        "fields": [
            "report_period",
            "announcement_date",
            "roe_ttm",
        ],
        "point_in_time": True,
    },
    "main_inflow_5d_ratio": {
        "tables": ["raw_fund_flows", "raw_daily_bars"],
        "fields": ["main_net_inflow", "amount"],
    },
    "lhb_institution_net_ratio": {
        "tables": ["raw_sentiment", "raw_daily_bars"],
        "fields": ["has_lhb", "lhb_institution_net", "amount"],
        "provider_scope": "institution_seats_only",
    },
    "turnover_z20": {
        "table": "raw_daily_bars",
        "field": "turnover_rate",
    },
    "hot_rank_attention": {
        "table": "raw_sentiment",
        "fields": ["hot_rank", "hot_rank_total", "hot_rank_pct"],
        "provider_scope": "current_top_100_only",
    },
    "tail_accumulation_proxy": {
        "table": "raw_tail_proxy",
        "fields": [
            "tail_activity_ratio",
            "tail_return",
            "close_location",
            "proxy_score",
        ],
        "provider_scope": "candidate_pool_only",
    },
}

_PARAMS = {
    "ep_ttm": {"valuation_max_age_days": 7, "positive_pe_only": True},
    "negative_pb": {"valuation_max_age_days": 7, "positive_pb_only": True},
    "roe_yoy_growth": {
        "same_period_years": 1,
        "announcement_date_cutoff": True,
        "unit": "percentage_point",
    },
    "main_inflow_5d_ratio": {"window": 5, "minimum_periods": 5},
    "lhb_institution_net_ratio": {
        "event_only": True,
        "non_event_policy": "missing",
        "model_enabled": False,
    },
    "turnover_z20": {"window": 20, "minimum_periods": 20},
    "hot_rank_attention": {
        "snapshot_only": True,
        "historical_backfill": False,
        "model_enabled": False,
    },
    "tail_accumulation_proxy": {
        "minute_period": "1",
        "tail_start": "14:30",
        "minimum_day_minutes": 180,
        "minimum_tail_minutes": 20,
        "level2": False,
        "model_enabled": False,
    },
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
                frequency=definition.frequency,
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
