"""Isolated deterministic market inputs; all factor/target/fit stages are real."""
from datetime import date, timedelta
import json
import math

from sqlalchemy import select

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorVersion
from app.models.factor_runtime import FactorSystemConfig
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factor_set_service import freeze_factor_set
from app.services.factors.definitions import seed_factor_definitions
from app.services.factors.factor_engine import calculate_stock_factors
from app.services.factors.ridge_model import train_rolling_ridge, ModelGate
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import calculate_targets
from tests.test_whitebox_factor_engine import _bar, _valuation


def build_real_factor_pipeline(db, tmp_path, *, days=100, symbol_count=12,
                              model_run_id="real-pipeline-model", end_date=None):
    """Calculate frozen ep_ttm v2 from valuation inputs and learn T+5 returns.

    Threshold overrides apply only to this temporary test database. The
    deterministic fixture has intentionally high IC, rather than a claimed
    real-market performance metric. No coefficients or model rows are seeded.
    """
    warehouse = FactorWarehouse(tmp_path / (model_run_id + ".duckdb"))
    first = date(2025, 1, 2)
    market_days = []
    candidate = first
    while len(market_days) < days:
        if candidate.weekday() < 5:
            market_days.append(candidate)
        candidate += timedelta(days=1)
    if end_date is not None:
        market_days = []
        candidate = end_date
        while len(market_days) < days:
            if candidate.weekday() < 5:
                market_days.append(candidate)
            candidate -= timedelta(days=1)
        market_days.reverse()
    seed_factor_definitions(db)
    factor = db.scalar(select(Factor).where(Factor.code == "ep_ttm"))
    factor.lifecycle_status = "active"
    initial = db.scalar(select(FactorVersion).where(FactorVersion.factor_id == factor.id, FactorVersion.version == 1))
    version = FactorVersion(
        factor_id=factor.id, version=2, formula_expr=initial.formula_expr,
        params_json=initial.params_json, source_mapping_json=initial.source_mapping_json,
        direction=initial.direction, validation_status="valid",
    )
    db.add(version)
    db.flush()
    factor_set = FactorSet(id=model_run_id + "-set", name="Real calculated v2", status="draft")
    db.add(factor_set)
    db.flush()
    db.add(FactorSetMember(
        factor_set_id=factor_set.id, factor_id=factor.id, factor_version_id=version.id,
        factor_code=factor.code, factor_version=2, role="feature",
        missing_policy="exclude", display_order=0,
    ))
    db.flush()
    factor_set = freeze_factor_set(db, factor_set.id, actor="isolated_test")
    db.add(FactorSystemConfig(
        id=1, feature_enabled=1, warehouse_path=str(warehouse.path),
        extra_json=json.dumps({"gates": {"coverage_threshold": 0.9, "ic_min": 0.0,
                                       "ic_max": 1.01, "min_sample_count": 100}}),
        updated_by="isolated_test",
    ))
    bars, valuations, symbols = [], [], []
    for index in range(symbol_count):
        code = f"{index + 1:06d}"
        beta = (index - (symbol_count - 1) / 2) / symbol_count
        daily_return = 0.002 * beta
        previous_close = 10.0
        symbol = Symbol(symbol=code, name=code, asset_type="stock", market="CN", is_active=1)
        db.add(symbol)
        db.flush()
        symbols.append(symbol)
        for day_index, day in enumerate(market_days):
            close = previous_close * math.exp(daily_return)
            bar = _bar(code, day, day_index * symbol_count + index + 1,
                       turnover_rate=1.0 + beta + (day_index % 7) / 10)
            bar.update(open=previous_close, close=close, high=max(previous_close, close) * 1.01,
                       low=min(previous_close, close) * 0.99)
            bars.append(bar)
            valuations.append(_valuation(code, day, 1 / (0.05 + 0.015 * beta), 2.0))
            previous_close = close
        db.add(Score(symbol_id=symbol.id, trade_date=market_days[-1], quality_score=50,
                     quality_grade="C", timing_score=50, priority_score=50,
                     stage="start", action="hold", weight_mode="manual",
                     calc_batch_id="real-base-scores", factor_scores_json="{}"))
    db.commit()
    warehouse.upsert_daily_bars(bars, source_key="isolated.market", watermark=len(bars))
    warehouse.upsert_records("raw_valuation_snapshots", valuations)
    factors = calculate_stock_factors(warehouse, db=db, factor_set_ids=(factor_set.id,),
                                     start_date=market_days[0], end_date=market_days[-1],
                                     calc_batch_id=model_run_id + "-factors")
    targets = calculate_targets(warehouse, start_date=market_days[0], end_date=market_days[-1],
                                calc_batch_id=model_run_id + "-targets")
    result = train_rolling_ridge(
        db, warehouse, factor_calc_batch_id=factors.calc_batch_id,
        target_calc_batch_id=targets.calc_batch_id, factor_set_id=factor_set.id,
        data_cutoff_date=market_days[-1], window_days=90, validation_days=20,
        gate=ModelGate(minimum_samples=100, minimum_symbols=5, minimum_trade_dates=60,
                       minimum_validation_dates=10, minimum_validation_ic=0.8),
        model_run_id=model_run_id,
    )
    db.commit()
    assert result.status == "validated", result.rejection_reasons
    return {"warehouse": warehouse, "factor_set": factor_set,
            "model_run_id": result.model_run_id, "training": result,
            "factors": factors, "targets": targets, "trade_date": market_days[-1],
            "symbols": symbols, "market_days": market_days}
