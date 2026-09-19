from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sklearn")

from app.models.factor_model import FactorModelRun
from app.services.factors.ridge_model import (
    FEATURE_CODES,
    ModelGate,
    evaluate_model_gate,
    train_rolling_ridge,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def _seed_training_data(
    warehouse: FactorWarehouse,
    *,
    trade_dates: int = 80,
    symbols: int = 10,
):
    start = date(2025, 1, 1)
    factor_rows = []
    target_rows = []
    created = datetime(2026, 1, 1)
    for day in range(trade_dates):
        signal_date = start + timedelta(days=day)
        for symbol_index in range(symbols):
            symbol = f"{symbol_index + 1:06d}"
            ep = (symbol_index - 4.5) / 3 + day * 0.005
            pb = ((symbol_index * 2 + day) % 9 - 4) / 2
            flow = ((symbol_index + day * 3) % 11 - 5) / 3
            turnover = ((symbol_index * 3 + day) % 7 - 3) / 2
            roe = ((symbol_index * 5 + day * 2) % 13 - 6) / 4
            features = {
                "ep_ttm": ep,
                "negative_pb": pb,
                "roe_yoy_growth": roe,
                "main_inflow_5d_ratio": flow,
                "turnover_z20": turnover,
            }
            target = (
                0.04 * ep
                - 0.025 * pb
                + 0.02 * roe
                + 0.015 * flow
                - 0.005 * turnover
                + symbol_index * 1e-5
            )
            for factor_code, value in features.items():
                factor_rows.append(
                    {
                        "symbol": symbol,
                        "trade_date": signal_date,
                        "factor_code": factor_code,
                        "factor_version": 1,
                        "raw_value": value,
                        "winsorized_value": value,
                        "normalized_value": value,
                        "is_imputed": False,
                        "imputation_method": None,
                        "eligible": True,
                        "data_cutoff_at": created,
                        "calc_batch_id": "factor-train",
                        "created_at": created,
                    }
                )
            target_rows.append(
                {
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "entry_date": signal_date + timedelta(days=1),
                    "exit_date": signal_date + timedelta(days=5),
                    "target_code": "target_5d_return",
                    "target_value": target,
                    "is_tradable": True,
                    "invalid_reason": None,
                    "calc_batch_id": "target-train",
                    "created_at": created,
                }
            )
    warehouse.upsert_records("factor_values", factor_rows)
    warehouse.upsert_records("factor_targets", target_rows)
    return start


def test_ridge_uses_time_split_signed_weights_and_is_idempotent(
    db_session, tmp_path
):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = _seed_training_data(warehouse)
    gate = ModelGate(
        minimum_samples=500,
        minimum_symbols=5,
        minimum_trade_dates=60,
        minimum_validation_dates=10,
        minimum_validation_ic=0.5,
    )

    first = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        data_cutoff_date=start + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )
    db_session.commit()
    second = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        data_cutoff_date=start + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )

    assert first.status == "validated"
    assert first.validation_ic is not None
    assert first.validation_ic > 0.9
    assert first.coefficients["ep_ttm"] > 0
    assert first.coefficients["negative_pb"] < 0
    assert first.coefficients["roe_yoy_growth"] > 0
    assert first.coefficients["main_inflow_5d_ratio"] > 0
    assert first.coefficients["turnover_z20"] < 0
    assert sum(abs(v) for v in first.normalized_weights.values()) == pytest.approx(1)
    assert second.reused is True
    assert second.model_run_id == first.model_run_id

    model = db_session.get(FactorModelRun, first.model_run_id)
    assert model is not None
    assert model.train_end_date < model.validation_start_date
    assert len(model.weights) == len(FEATURE_CODES)


def test_ridge_excludes_targets_after_cutoff(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = _seed_training_data(warehouse, trade_dates=20, symbols=3)
    cutoff = start + timedelta(days=15)

    result = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        data_cutoff_date=cutoff,
        window_days=15,
        validation_days=5,
        gate=ModelGate(
            minimum_samples=1,
            minimum_symbols=1,
            minimum_trade_dates=1,
            minimum_validation_dates=1,
            minimum_validation_ic=-1,
        ),
        model_run_id="cutoff-test",
    )

    assert result.sample_count == 33
    model = db_session.get(FactorModelRun, "cutoff-test")
    assert model is not None
    assert model.data_cutoff_at.date() == cutoff


def test_ridge_rejects_insufficient_sample_without_publishing(
    db_session, tmp_path
):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = _seed_training_data(warehouse, trade_dates=8, symbols=2)

    result = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        data_cutoff_date=start + timedelta(days=20),
        window_days=10,
        validation_days=3,
        model_run_id="rejected-test",
    )

    assert result.status == "rejected"
    assert any(item.startswith("sample_count:") for item in result.rejection_reasons)
    assert db_session.get(FactorModelRun, "rejected-test").status == "rejected"


def test_model_gate_rejects_non_finite_or_zero_coefficients():
    gate = ModelGate(
        minimum_samples=1,
        minimum_symbols=1,
        minimum_trade_dates=1,
        minimum_validation_dates=1,
        minimum_validation_ic=0,
    )
    non_finite = evaluate_model_gate(
        sample_count=1,
        symbol_count=1,
        trade_date_count=1,
        validation_date_count=1,
        validation_ic=0.1,
        coefficients=[1, np.nan, *([1] * (len(FEATURE_CODES) - 2))],
        gate=gate,
    )
    zero = evaluate_model_gate(
        sample_count=1,
        symbol_count=1,
        trade_date_count=1,
        validation_date_count=1,
        validation_ic=0.1,
        coefficients=[0] * len(FEATURE_CODES),
        gate=gate,
    )

    assert "coefficients:non_finite" in non_finite
    assert "coefficients:all_zero" in zero


def test_ridge_validates_parameters(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    with pytest.raises(ValueError, match="window_days"):
        train_rolling_ridge(
            db_session,
            warehouse,
            factor_calc_batch_id="x",
            target_calc_batch_id="y",
            window_days=1,
        )
    with pytest.raises(ValueError, match="validation_days"):
        train_rolling_ridge(
            db_session,
            warehouse,
            factor_calc_batch_id="x",
            target_calc_batch_id="y",
            window_days=10,
            validation_days=10,
        )
