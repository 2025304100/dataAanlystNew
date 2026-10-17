"""Explicit deterministic fit-label policies for linear score models.

Policies transform training labels only. Persisted model inference continues to
use the original normalized factor features and a scalar intercept.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


RAW_RETURN_POLICY = "raw_return_v2"
CROSS_SECTIONAL_RANK_POLICY = "cross_sectional_rank_v1"
CROSS_SECTIONAL_RANK_PROTOCOL = "ridge-temporal-cross-sectional-rank-v1"
CROSS_SECTIONAL_RANK_DEFINITION = {
    "target": "within_trade_date_average_rank_divided_by_n_plus_one_minus_half",
    "ties": "average",
    "sample_weight": "n_rows / (n_dates * date_row_count)",
    "weight_normalization": "mean_one",
    "tuning_metric": "equal_date_mean_squared_error_on_fit_target",
    "validation_target": "original_untransformed_target_5d_return",
    "validation_ic": "pooled_spearman",
    "inference": "unchanged_normalized_features_linear_coefficients_and_intercept",
}


@dataclass(frozen=True)
class FitTarget:
    target: np.ndarray
    sample_weight: np.ndarray | None


def resolve_fit_policy(hyperparameters: dict[str, Any] | None = None,
                       fit_policy: str | None = None) -> str:
    """Resolve an opt-in policy without silently accepting unknown strategies."""
    if hyperparameters is not None and not isinstance(hyperparameters, dict):
        raise ValueError("fit_hyperparameters_must_be_an_object")
    if hyperparameters is not None and "fit_policy" in hyperparameters:
        supplied = hyperparameters["fit_policy"]
        if fit_policy is not None and fit_policy != supplied:
            raise ValueError("fit_policy_conflict")
        fit_policy = supplied
    elif fit_policy is None:
        fit_policy = RAW_RETURN_POLICY
    if not isinstance(fit_policy, str) or fit_policy not in {
        RAW_RETURN_POLICY, CROSS_SECTIONAL_RANK_POLICY,
    }:
        raise ValueError(f"unsupported_fit_policy:{fit_policy!r}")
    return fit_policy


def prepare_fit_target(frame: pd.DataFrame, fit_policy: str | None = None) -> FitTarget:
    """Prepare aligned fit labels/weights; never mutate features or raw labels."""
    policy = resolve_fit_policy(fit_policy=fit_policy)
    if not {"trade_date", "target"}.issubset(frame.columns):
        raise ValueError("fit_target_missing_columns")
    target = frame["target"].to_numpy(dtype=float, copy=True)
    if not np.isfinite(target).all() or frame["trade_date"].isna().any():
        raise ValueError("fit_target_non_finite_or_missing_dates")
    if policy == RAW_RETURN_POLICY:
        return FitTarget(target, None)
    if frame.empty:
        return FitTarget(target, np.asarray([], dtype=float))
    grouped = frame.groupby("trade_date", sort=False)["target"]
    count = grouped.transform("size").to_numpy(dtype=float)
    rank = grouped.rank(method="average").to_numpy(dtype=float)
    weights = len(frame) / (frame["trade_date"].nunique() * count)
    return FitTarget(rank / (count + 1.0) - 0.5, weights)
