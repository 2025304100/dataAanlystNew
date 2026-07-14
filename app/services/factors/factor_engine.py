"""Vectorized, local-only factor calculation and cross-sectional scaling."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from uuid import uuid4

import numpy as np
import pandas as pd

from app.services.factors.definitions import FACTOR_DEFINITIONS
from app.services.factors.store import FactorWarehouse


@dataclass
class FactorCalculationResult:
    calc_batch_id: str
    rows_written: int = 0
    eligible_rows: int = 0
    trade_date_count: int = 0
    symbol_count: int = 0
    coverage_by_factor: dict[str, float] = field(default_factory=dict)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def winsorize_cross_section(
    values: pd.Series,
    *,
    mad_multiplier: float = 3.0,
    lower_quantile: float = 0.01,
    upper_quantile: float = 0.99,
    epsilon: float = 1e-8,
) -> pd.Series:
    """MAD winsorization with a quantile fallback for zero-MAD sections."""
    result = pd.to_numeric(values, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    valid = result.dropna()
    if valid.empty:
        return result
    median = float(valid.median())
    mad = float((valid - median).abs().median())
    if mad > epsilon:
        scale = 1.4826 * mad
        lower = median - mad_multiplier * scale
        upper = median + mad_multiplier * scale
    elif len(valid) > 1:
        lower = float(valid.quantile(lower_quantile))
        upper = float(valid.quantile(upper_quantile))
    else:
        lower = upper = float(valid.iloc[0])
    result.loc[valid.index] = valid.clip(lower=lower, upper=upper)
    return result


def zscore_cross_section(
    values: pd.Series, *, epsilon: float = 1e-8
) -> pd.Series:
    """Population z-score that maps a zero-variance section to zero."""
    result = pd.to_numeric(values, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    valid = result.dropna()
    if valid.empty:
        return result
    mean = float(valid.mean())
    std = float(valid.std(ddof=0))
    if not np.isfinite(std) or std <= epsilon:
        result.loc[valid.index] = 0.0
    else:
        result.loc[valid.index] = (valid - mean) / (std + epsilon)
    return result


def _load_raw_factor_panel(
    warehouse: FactorWarehouse,
    *,
    adjust: str,
    valuation_max_age_days: int,
) -> pd.DataFrame:
    sql = """
        WITH valuation_dedup AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, trade_date
                       ORDER BY ingested_at DESC, source
                   ) AS row_num
            FROM raw_valuation_snapshots
        ),
        flow_dedup AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, trade_date
                       ORDER BY ingested_at DESC, source
                   ) AS row_num
            FROM raw_fund_flows
        ),
        panel AS (
            SELECT
                b.symbol,
                b.trade_date,
                b.amount,
                b.turnover_rate,
                v.trade_date AS valuation_date,
                v.pe_ttm,
                v.pb,
                f.main_net_inflow
            FROM raw_daily_bars b
            LEFT JOIN LATERAL (
                SELECT trade_date, pe_ttm, pb
                FROM valuation_dedup v
                WHERE v.row_num = 1
                  AND v.symbol = b.symbol
                  AND v.trade_date <= b.trade_date
                ORDER BY v.trade_date DESC
                LIMIT 1
            ) v ON TRUE
            LEFT JOIN flow_dedup f
              ON f.row_num = 1
             AND f.symbol = b.symbol
             AND f.trade_date = b.trade_date
            WHERE b.adjust = ?
        ),
        rolling AS (
            SELECT
                *,
                COUNT(*) OVER five_days AS bar_count_5,
                COUNT(amount) OVER five_days AS amount_count_5,
                COUNT(main_net_inflow) OVER five_days AS flow_count_5,
                SUM(amount) OVER five_days AS amount_sum_5,
                SUM(main_net_inflow) OVER five_days AS flow_sum_5,
                COUNT(turnover_rate) OVER twenty_days AS turnover_count_20,
                AVG(turnover_rate) OVER twenty_days AS turnover_mean_20,
                STDDEV_POP(turnover_rate) OVER twenty_days AS turnover_std_20
            FROM panel
            WINDOW
                five_days AS (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ),
                twenty_days AS (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                )
        )
        SELECT
            symbol,
            trade_date,
            CASE
                WHEN pe_ttm > 0
                 AND valuation_date IS NOT NULL
                 AND DATE_DIFF('day', valuation_date, trade_date) <= ?
                THEN 1.0 / pe_ttm
                ELSE NULL
            END AS ep_ttm,
            CASE
                WHEN pb > 0
                 AND valuation_date IS NOT NULL
                 AND DATE_DIFF('day', valuation_date, trade_date) <= ?
                THEN -pb
                ELSE NULL
            END AS negative_pb,
            CASE
                WHEN bar_count_5 = 5
                 AND amount_count_5 = 5
                 AND flow_count_5 = 5
                 AND ABS(amount_sum_5) > 1e-8
                THEN flow_sum_5 / amount_sum_5
                ELSE NULL
            END AS main_inflow_5d_ratio,
            CASE
                WHEN turnover_count_20 = 20
                 AND turnover_std_20 > 1e-8
                THEN (turnover_rate - turnover_mean_20) / turnover_std_20
                WHEN turnover_count_20 = 20
                THEN 0.0
                ELSE NULL
            END AS turnover_z20
        FROM rolling
        ORDER BY trade_date, symbol
    """
    with warehouse.connection(read_only=True) as conn:
        return conn.execute(
            sql,
            [adjust, valuation_max_age_days, valuation_max_age_days],
        ).fetchdf()


def _long_factor_frame(panel: pd.DataFrame) -> pd.DataFrame:
    factor_codes = [definition.code for definition in FACTOR_DEFINITIONS]
    if panel.empty:
        return pd.DataFrame(
            columns=["symbol", "trade_date", "factor_code", "raw_value"]
        )
    return panel.melt(
        id_vars=["symbol", "trade_date"],
        value_vars=factor_codes,
        var_name="factor_code",
        value_name="raw_value",
    )


def _standardize(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["raw_value"] = pd.to_numeric(
        result["raw_value"], errors="coerce"
    ).replace([np.inf, -np.inf], np.nan)
    result["winsorized_value"] = np.nan
    result["normalized_value"] = np.nan
    for _, index in result.groupby(
        ["trade_date", "factor_code"], sort=False
    ).groups.items():
        winsorized = winsorize_cross_section(result.loc[index, "raw_value"])
        result.loc[index, "winsorized_value"] = winsorized
        result.loc[index, "normalized_value"] = zscore_cross_section(
            winsorized
        )
    result["eligible"] = result["raw_value"].notna()
    return result


def calculate_stock_factors(
    warehouse: FactorWarehouse,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    calc_batch_id: str | None = None,
    adjust: str = "qfq",
    valuation_max_age_days: int = 7,
) -> FactorCalculationResult:
    """Calculate and persist the first four stock factors from local data."""
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if valuation_max_age_days < 0:
        raise ValueError("valuation_max_age_days must not be negative")
    warehouse.initialize()
    batch_id = calc_batch_id or f"factors-{uuid4().hex}"
    panel = _load_raw_factor_panel(
        warehouse,
        adjust=adjust,
        valuation_max_age_days=valuation_max_age_days,
    )
    if not panel.empty:
        panel["trade_date"] = pd.to_datetime(
            panel["trade_date"], errors="coerce"
        ).dt.date
        if start_date is not None:
            panel = panel[panel["trade_date"] >= start_date]
        if end_date is not None:
            panel = panel[panel["trade_date"] <= end_date]
    long_frame = _standardize(_long_factor_frame(panel))
    cutoff = _utcnow_naive()
    versions = {
        definition.code: definition.version
        for definition in FACTOR_DEFINITIONS
    }
    records = []
    for row in long_frame.to_dict("records"):
        eligible = bool(row["eligible"])
        records.append(
            {
                "symbol": row["symbol"],
                "trade_date": row["trade_date"],
                "factor_code": row["factor_code"],
                "factor_version": versions[row["factor_code"]],
                "raw_value": (
                    float(row["raw_value"]) if eligible else None
                ),
                "winsorized_value": (
                    float(row["winsorized_value"]) if eligible else None
                ),
                "normalized_value": (
                    float(row["normalized_value"]) if eligible else None
                ),
                "is_imputed": False,
                "imputation_method": None,
                "eligible": eligible,
                "data_cutoff_at": cutoff,
                "calc_batch_id": batch_id,
                "created_at": cutoff,
            }
        )
    rows_written = warehouse.upsert_records("factor_values", records)
    total_by_factor = (
        long_frame.groupby("factor_code").size().to_dict()
        if not long_frame.empty
        else {}
    )
    eligible_by_factor = (
        long_frame.groupby("factor_code")["eligible"].sum().to_dict()
        if not long_frame.empty
        else {}
    )
    coverage = {
        definition.code: round(
            float(eligible_by_factor.get(definition.code, 0))
            / max(int(total_by_factor.get(definition.code, 0)), 1),
            6,
        )
        for definition in FACTOR_DEFINITIONS
    }
    return FactorCalculationResult(
        calc_batch_id=batch_id,
        rows_written=rows_written,
        eligible_rows=int(long_frame["eligible"].sum())
        if not long_frame.empty
        else 0,
        trade_date_count=int(long_frame["trade_date"].nunique())
        if not long_frame.empty
        else 0,
        symbol_count=int(long_frame["symbol"].nunique())
        if not long_frame.empty
        else 0,
        coverage_by_factor=coverage,
    )
