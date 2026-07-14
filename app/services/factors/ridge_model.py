"""Rolling Ridge training with time-based validation and release gates."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Iterable

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error, r2_score
from sqlalchemy.orm import Session

from app.models.factor_model import (
    FactorModelRun,
    FactorWeightSnapshot,
)
from app.services.factors.definitions import FACTOR_DEFINITIONS
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import TARGET_CODE


FEATURE_CODES = tuple(item.code for item in FACTOR_DEFINITIONS)


@dataclass(frozen=True)
class ModelGate:
    minimum_samples: int = 500
    minimum_symbols: int = 20
    minimum_trade_dates: int = 60
    minimum_validation_dates: int = 20
    minimum_validation_ic: float = 0.0


@dataclass
class RidgeTrainingResult:
    model_run_id: str
    status: str
    selected_alpha: float | None
    sample_count: int
    symbol_count: int
    trade_date_count: int
    validation_ic: float | None
    coefficients: dict[str, float] = field(default_factory=dict)
    normalized_weights: dict[str, float] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    reused: bool = False


def _safe_correlation(
    left: pd.Series, right: pd.Series, *, method: str = "spearman"
) -> float | None:
    value = left.corr(right, method=method)
    return float(value) if value is not None and np.isfinite(value) else None


def evaluate_model_gate(
    *,
    sample_count: int,
    symbol_count: int,
    trade_date_count: int,
    validation_date_count: int,
    validation_ic: float | None,
    coefficients: Iterable[float] | None,
    gate: ModelGate,
) -> list[str]:
    reasons = []
    if sample_count < gate.minimum_samples:
        reasons.append(
            f"sample_count:{sample_count}<{gate.minimum_samples}"
        )
    if symbol_count < gate.minimum_symbols:
        reasons.append(
            f"symbol_count:{symbol_count}<{gate.minimum_symbols}"
        )
    if trade_date_count < gate.minimum_trade_dates:
        reasons.append(
            f"trade_date_count:{trade_date_count}<{gate.minimum_trade_dates}"
        )
    if validation_date_count < gate.minimum_validation_dates:
        reasons.append(
            "validation_date_count:"
            f"{validation_date_count}<{gate.minimum_validation_dates}"
        )
    if validation_ic is None or not np.isfinite(validation_ic):
        reasons.append("validation_ic:non_finite")
    elif validation_ic < gate.minimum_validation_ic:
        reasons.append(
            f"validation_ic:{validation_ic:.6f}<"
            f"{gate.minimum_validation_ic:.6f}"
        )
    if coefficients is None:
        reasons.append("coefficients:missing")
    else:
        coefficient_array = np.asarray(list(coefficients), dtype=float)
        if coefficient_array.size != len(FEATURE_CODES):
            reasons.append("coefficients:wrong_size")
        elif not np.isfinite(coefficient_array).all():
            reasons.append("coefficients:non_finite")
        elif float(np.abs(coefficient_array).sum()) <= 1e-12:
            reasons.append("coefficients:all_zero")
    return reasons


def _load_samples(
    warehouse: FactorWarehouse,
    *,
    factor_calc_batch_id: str,
    target_calc_batch_id: str,
    data_cutoff_date: date,
) -> pd.DataFrame:
    with warehouse.connection(read_only=True) as conn:
        factors = conn.execute(
            """
            SELECT
                symbol,
                trade_date,
                factor_code,
                factor_version,
                normalized_value
            FROM factor_values
            WHERE calc_batch_id = ?
              AND eligible
              AND factor_code IN (?, ?, ?, ?)
              AND trade_date <= ?
            """,
            [
                factor_calc_batch_id,
                *FEATURE_CODES,
                data_cutoff_date,
            ],
        ).fetchdf()
        targets = conn.execute(
            """
            SELECT
                symbol,
                signal_date,
                exit_date,
                target_value
            FROM factor_targets
            WHERE calc_batch_id = ?
              AND target_code = ?
              AND is_tradable
              AND target_value IS NOT NULL
              AND exit_date <= ?
            """,
            [target_calc_batch_id, TARGET_CODE, data_cutoff_date],
        ).fetchdf()
    if factors.empty or targets.empty:
        return pd.DataFrame(
            columns=["symbol", "trade_date", *FEATURE_CODES, "target"]
        )
    factors["trade_date"] = pd.to_datetime(
        factors["trade_date"], errors="coerce"
    ).dt.date
    targets["signal_date"] = pd.to_datetime(
        targets["signal_date"], errors="coerce"
    ).dt.date
    pivot = factors.pivot_table(
        index=["symbol", "trade_date"],
        columns="factor_code",
        values="normalized_value",
        aggfunc="last",
    ).reset_index()
    merged = pivot.merge(
        targets,
        left_on=["symbol", "trade_date"],
        right_on=["symbol", "signal_date"],
        how="inner",
    )
    merged = merged.rename(columns={"target_value": "target"})
    for feature in FEATURE_CODES:
        if feature not in merged.columns:
            merged[feature] = np.nan
    return merged[
        ["symbol", "trade_date", *FEATURE_CODES, "target", "exit_date"]
    ].dropna(subset=[*FEATURE_CODES, "target"])


def _run_id(payload: dict) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f"ridge-{digest}"


def _result_from_model(model: FactorModelRun) -> RidgeTrainingResult:
    metrics = json.loads(model.metrics_json or "{}")
    hyperparameters = json.loads(model.hyperparameters_json or "{}")
    coefficients = {
        weight.factor_code: float(weight.coefficient)
        for weight in model.weights
    }
    normalized_weights = {
        weight.factor_code: float(weight.normalized_weight)
        for weight in model.weights
    }
    return RidgeTrainingResult(
        model_run_id=model.id,
        status=model.status,
        selected_alpha=hyperparameters.get("selected_alpha"),
        sample_count=model.sample_count,
        symbol_count=model.symbol_count,
        trade_date_count=model.trade_date_count,
        validation_ic=metrics.get("validation_ic"),
        coefficients=coefficients,
        normalized_weights=normalized_weights,
        rejection_reasons=(
            model.rejection_reason.split(";")
            if model.rejection_reason
            else []
        ),
        reused=True,
    )


def train_rolling_ridge(
    db: Session,
    warehouse: FactorWarehouse,
    *,
    factor_calc_batch_id: str,
    target_calc_batch_id: str,
    data_cutoff_date: date | None = None,
    window_days: int = 250,
    validation_days: int = 50,
    alphas: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0),
    gate: ModelGate | None = None,
    model_run_id: str | None = None,
) -> RidgeTrainingResult:
    """Train an immutable candidate model; never activate it automatically."""
    if window_days < 2:
        raise ValueError("window_days must be at least 2")
    if validation_days < 1 or validation_days >= window_days:
        raise ValueError("validation_days must be within the training window")
    if not alphas or any(alpha <= 0 for alpha in alphas):
        raise ValueError("alphas must contain positive values")
    active_gate = gate or ModelGate()
    cutoff = data_cutoff_date or date.today()
    identity = {
        "factor_calc_batch_id": factor_calc_batch_id,
        "target_calc_batch_id": target_calc_batch_id,
        "data_cutoff_date": cutoff,
        "window_days": window_days,
        "validation_days": validation_days,
        "alphas": alphas,
        "features": FEATURE_CODES,
    }
    run_id = model_run_id or _run_id(identity)
    existing = db.get(FactorModelRun, run_id)
    if existing is not None:
        return _result_from_model(existing)

    samples = _load_samples(
        warehouse,
        factor_calc_batch_id=factor_calc_batch_id,
        target_calc_batch_id=target_calc_batch_id,
        data_cutoff_date=cutoff,
    )
    all_dates = sorted(samples["trade_date"].unique()) if not samples.empty else []
    window_dates = all_dates[-window_days:]
    samples = samples[samples["trade_date"].isin(window_dates)].copy()
    validation_dates = window_dates[-validation_days:]
    training_dates = window_dates[:-validation_days]
    train = samples[samples["trade_date"].isin(training_dates)]
    validation = samples[samples["trade_date"].isin(validation_dates)]
    sample_count = len(samples)
    symbol_count = int(samples["symbol"].nunique()) if not samples.empty else 0
    trade_date_count = len(window_dates)

    coefficients: dict[str, float] = {}
    normalized_weights: dict[str, float] = {}
    selected_alpha = None
    validation_ic = None
    metrics: dict[str, float | int | None] = {
        "validation_ic": None,
        "validation_mse": None,
        "validation_r2": None,
        "train_ic": None,
        "sample_count": sample_count,
        "validation_sample_count": len(validation),
        "validation_date_count": len(validation_dates),
    }
    final_intercept = None
    if training_dates and validation_dates and not train.empty and not validation.empty:
        x_train = train[list(FEATURE_CODES)].to_numpy(dtype=float)
        y_train = train["target"].to_numpy(dtype=float)
        x_validation = validation[list(FEATURE_CODES)].to_numpy(dtype=float)
        y_validation = validation["target"].to_numpy(dtype=float)
        candidates = []
        for alpha in alphas:
            candidate = Ridge(alpha=float(alpha), fit_intercept=True)
            candidate.fit(x_train, y_train)
            prediction = candidate.predict(x_validation)
            candidates.append(
                (
                    float(mean_squared_error(y_validation, prediction)),
                    float(alpha),
                    candidate,
                    prediction,
                )
            )
        validation_mse, selected_alpha, selected, validation_prediction = min(
            candidates, key=lambda item: (item[0], item[1])
        )
        validation_ic = _safe_correlation(
            pd.Series(validation_prediction), validation["target"].reset_index(drop=True)
        )
        train_prediction = selected.predict(x_train)
        train_ic = _safe_correlation(
            pd.Series(train_prediction), train["target"].reset_index(drop=True)
        )
        validation_r2 = (
            float(r2_score(y_validation, validation_prediction))
            if len(validation) >= 2
            else None
        )
        final_model = Ridge(alpha=selected_alpha, fit_intercept=True)
        final_model.fit(
            samples[list(FEATURE_CODES)].to_numpy(dtype=float),
            samples["target"].to_numpy(dtype=float),
        )
        final_intercept = float(final_model.intercept_)
        coefficients = {
            code: float(value)
            for code, value in zip(FEATURE_CODES, final_model.coef_)
        }
        absolute_sum = sum(abs(value) for value in coefficients.values())
        if absolute_sum > 1e-12:
            normalized_weights = {
                code: value / absolute_sum
                for code, value in coefficients.items()
            }
        metrics.update(
            {
                "validation_ic": validation_ic,
                "validation_mse": validation_mse,
                "validation_r2": validation_r2,
                "train_ic": train_ic,
            }
        )

    rejection_reasons = evaluate_model_gate(
        sample_count=sample_count,
        symbol_count=symbol_count,
        trade_date_count=trade_date_count,
        validation_date_count=len(validation_dates),
        validation_ic=validation_ic,
        coefficients=coefficients.values() if coefficients else None,
        gate=active_gate,
    )
    status = "rejected" if rejection_reasons else "validated"
    feature_versions = {
        definition.code: definition.version
        for definition in FACTOR_DEFINITIONS
    }
    model = FactorModelRun(
        id=run_id,
        model_type="ridge",
        asset_type="stock",
        target_code=TARGET_CODE,
        train_start_date=training_dates[0] if training_dates else None,
        train_end_date=training_dates[-1] if training_dates else None,
        validation_start_date=validation_dates[0]
        if validation_dates
        else None,
        validation_end_date=validation_dates[-1]
        if validation_dates
        else None,
        data_cutoff_at=datetime.combine(cutoff, time.max),
        feature_versions_json=json.dumps(feature_versions, sort_keys=True),
        hyperparameters_json=json.dumps(
            {
                **identity,
                "selected_alpha": selected_alpha,
                "final_intercept": final_intercept,
            },
            sort_keys=True,
            default=str,
        ),
        metrics_json=json.dumps(metrics, sort_keys=True),
        sample_count=sample_count,
        symbol_count=symbol_count,
        trade_date_count=trade_date_count,
        status=status,
        rejection_reason=";".join(rejection_reasons) or None,
    )
    db.add(model)
    if coefficients:
        for feature in FEATURE_CODES:
            model.weights.append(
                FactorWeightSnapshot(
                    factor_code=feature,
                    factor_version=feature_versions[feature],
                    coefficient=coefficients[feature],
                    normalized_weight=normalized_weights.get(feature, 0.0),
                    train_ic=_safe_correlation(
                        train[feature], train["target"]
                    )
                    if not train.empty
                    else None,
                    validation_ic=_safe_correlation(
                        validation[feature], validation["target"]
                    )
                    if not validation.empty
                    else None,
                )
            )
    db.flush()
    return RidgeTrainingResult(
        model_run_id=run_id,
        status=status,
        selected_alpha=selected_alpha,
        sample_count=sample_count,
        symbol_count=symbol_count,
        trade_date_count=trade_date_count,
        validation_ic=validation_ic,
        coefficients=coefficients,
        normalized_weights=normalized_weights,
        rejection_reasons=rejection_reasons,
    )
