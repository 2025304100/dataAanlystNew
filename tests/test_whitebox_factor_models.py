from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models.factor import Factor
from app.models.factor_model import (
    FactorModelRun,
    FactorVersion,
    FactorWeightSnapshot,
)
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.definitions import seed_factor_definitions

pytestmark = pytest.mark.whitebox


def test_factor_model_metadata_and_score_defaults(db_session):
    factor = Factor(
        code="ep_ttm",
        name="E/P",
        category="fundamental",
        direction="positive",
        status="active",
        source_type="akshare",
        frequency="daily",
    )
    symbol = Symbol(
        symbol="600519",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
    )
    db_session.add_all([factor, symbol])
    db_session.flush()

    version = FactorVersion(
        factor_id=factor.id,
        version=1,
        formula_expr="1 / pe_ttm",
        params_json='{"winsorize":"mad"}',
        source_mapping_json='{"api":"stock_value_em"}',
        change_note="initial",
    )
    model = FactorModelRun(
        id="ridge-test-1",
        train_start_date=date(2025, 1, 1),
        train_end_date=date(2026, 1, 1),
        data_cutoff_at=datetime(2026, 1, 1, 18, 0),
        status="validated",
    )
    model.weights.append(
        FactorWeightSnapshot(
            factor_code="ep_ttm",
            factor_version=1,
            coefficient=0.25,
            normalized_weight=0.25,
        )
    )
    score = Score(
        symbol_id=symbol.id,
        trade_date=date(2026, 1, 2),
        quality_score=60,
        quality_grade="B",
        timing_score=55,
        stage="start",
        action="open",
        priority_score=58,
        calc_batch_id="manual-test",
    )
    db_session.add_all([version, model, score])
    db_session.commit()

    db_session.refresh(score)
    loaded = db_session.get(FactorModelRun, "ridge-test-1")

    assert score.weight_mode == "manual"
    assert loaded is not None
    assert loaded.status == "validated"
    assert len(loaded.weights) == 1
    assert loaded.weights[0].coefficient == 0.25


def test_seed_factor_definitions_is_idempotent(db_session):
    first = seed_factor_definitions(db_session)
    db_session.commit()
    second = seed_factor_definitions(db_session)
    db_session.commit()

    assert first == 16
    assert second == 0
    factors = db_session.query(Factor).all()
    versions = db_session.query(FactorVersion).all()
    assert {factor.code for factor in factors} == {
        "ep_ttm",
        "negative_pb",
        "roe_yoy_growth",
        "main_inflow_5d_ratio",
        "lhb_institution_net_ratio",
        "hot_rank_attention",
        "tail_accumulation_proxy",
        "turnover_z20",
    }
    assert len(versions) == 8
    assert {version.direction for version in versions} == {
        "higher_better",
        "nonlinear",
    }
