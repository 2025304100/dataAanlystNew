"""Vectorized, local-only factor calculation and cross-sectional scaling."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from datetime import date, datetime, timezone
from uuid import uuid4
from typing import Any

import numpy as np
import pandas as pd

from app.services.factors.definitions import FACTOR_DEFINITIONS
from app.services.feature_policy import DISABLED_FACTOR_CODES, reject_tail_proxy
from app.services.factors.store import FactorWarehouse


@dataclass
class FactorCalculationResult:
    calc_batch_id: str
    rows_written: int = 0
    eligible_rows: int = 0
    trade_date_count: int = 0
    symbol_count: int = 0
    coverage_by_factor: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class FrozenFactorSpec:
    """Detached, validated input contract; warehouse work never lazy-loads SQL."""
    code: str
    version: int
    version_id: int
    missing_policy: str
    native: bool
    execution_plan: Any | None
    source_tables: frozenset[str]


def resolve_frozen_factor_specs(db, factor_set_ids: tuple[str, ...], *,
                                valuation_max_age_days: int = 7) -> tuple[FrozenFactorSpec, ...]:
    from app.models.factor_evaluation import FactorSet
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion
    from app.services.factors.definitions import _SOURCE_MAPPINGS
    from app.services.factors.factor_compiler import FactorCompiler
    from app.services.factors.version_compatibility import (
        frozen_postprocess_supported, frozen_source_mapping_supported,
        native_factor_contract_matches,
    )
    resolved, seen = [], set()
    for set_id in dict.fromkeys(factor_set_ids):
        factor_set = db.get(FactorSet, set_id)
        if factor_set is None or factor_set.status != "frozen":
            raise ValueError(f"factor_set_not_frozen:{set_id}")
        for member in factor_set.members:
            if member.role != "feature":
                continue
            if member.factor_code in DISABLED_FACTOR_CODES:
                reject_tail_proxy()
            version = db.get(FactorVersion, member.factor_version_id)
            factor = db.get(Factor, member.factor_id)
            if (version is None or version.factor_id != member.factor_id
                    or version.version != member.factor_version or factor is None
                    or factor.code != member.factor_code):
                raise ValueError(f"factor_version_binding_mismatch:{member.factor_code}")
            key = (member.factor_code, int(version.version))
            if key in seen:
                continue
            params = json.loads(version.params_json or "{}")
            source = json.loads(version.source_mapping_json or "{}")
            postprocess = json.loads(version.postprocess_json or "null")
            native = native_factor_contract_matches(member.factor_code,
                formula=version.formula_expr, params=params, source_mapping=source,
                direction=version.direction, postprocess=postprocess,
                valuation_max_age_days=valuation_max_age_days)
            plan = None
            if native:
                mapping = _SOURCE_MAPPINGS[member.factor_code]
                tables = frozenset(mapping.get("tables") or [mapping["table"]])
            else:
                if not frozen_postprocess_supported(postprocess):
                    raise ValueError(f"unsupported_frozen_postprocess:{member.factor_code}:v{version.version}")
                compiled = FactorCompiler().compile(formula=version.formula_expr,
                    params=params, direction=version.direction, postprocess=postprocess or {
                        "winsorize": {"method": "mad"}, "zscore": {"ddof": 0},
                        "missing_policy": member.missing_policy,
                    })
                if not compiled.success or compiled.execution_plan is None:
                    reasons = ";".join(error.error_code for error in compiled.errors)
                    raise ValueError(f"unsupported_frozen_factor:{member.factor_code}:v{version.version}:{reasons}")
                plan = compiled.execution_plan
                if not frozen_source_mapping_supported(source, plan.data_dependencies):
                    raise ValueError(f"unsupported_frozen_source_mapping:{member.factor_code}:v{version.version}")
                tables = frozenset(plan.data_dependencies.get("source_tables", []))
            if member.missing_policy not in ("exclude", "ignore", "impute_zero"):
                raise ValueError(f"unsupported_frozen_missing_policy:{member.factor_code}:{member.missing_policy}")
            resolved.append(FrozenFactorSpec(member.factor_code, int(version.version),
                int(version.id), member.missing_policy, native, plan, tables))
            seen.add(key)
    return tuple(resolved)


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
    start_date: date | None = None,
    end_date: date | None = None,
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
        financial_dedup AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY
                           symbol,
                           report_period,
                           announcement_date,
                           report_type,
                           source
                       ORDER BY ingested_at DESC
                   ) AS row_num
            FROM raw_financial_reports
            WHERE report_type = 'financial_analysis'
        ),
        sentiment_dedup AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, trade_date
                       ORDER BY ingested_at DESC, source
                   ) AS row_num
            FROM raw_sentiment
            WHERE has_lhb
              AND lhb_institution_net IS NOT NULL
        ),
        hot_rank_dedup AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, trade_date
                       ORDER BY ingested_at DESC, source
                   ) AS row_num
            FROM raw_sentiment
            WHERE hot_rank_pct IS NOT NULL
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
                f.main_net_inflow,
                s.lhb_institution_net,
                h.hot_rank_pct,
                current_fin.roe_ttm AS current_roe_ttm,
                prior_fin.roe_ttm AS prior_year_roe_ttm
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
            LEFT JOIN sentiment_dedup s
              ON s.row_num = 1
             AND s.symbol = b.symbol
             AND s.trade_date = b.trade_date
            LEFT JOIN hot_rank_dedup h
              ON h.row_num = 1
             AND h.symbol = b.symbol
             AND h.trade_date = b.trade_date
            LEFT JOIN LATERAL (
                SELECT
                    report_period,
                    announcement_date,
                    roe_ttm
                FROM financial_dedup current_fin
                WHERE current_fin.row_num = 1
                  AND current_fin.symbol = b.symbol
                  AND current_fin.announcement_date <= b.trade_date
                  AND current_fin.roe_ttm IS NOT NULL
                ORDER BY
                    current_fin.report_period DESC,
                    current_fin.announcement_date DESC,
                    current_fin.ingested_at DESC,
                    current_fin.source
                LIMIT 1
            ) current_fin ON TRUE
            LEFT JOIN LATERAL (
                SELECT roe_ttm
                FROM financial_dedup prior_fin
                WHERE prior_fin.row_num = 1
                  AND prior_fin.symbol = b.symbol
                  AND prior_fin.announcement_date <= b.trade_date
                  AND prior_fin.roe_ttm IS NOT NULL
                  AND EXTRACT(YEAR FROM prior_fin.report_period)
                      = EXTRACT(YEAR FROM current_fin.report_period) - 1
                  AND EXTRACT(MONTH FROM prior_fin.report_period)
                      = EXTRACT(MONTH FROM current_fin.report_period)
                  AND EXTRACT(DAY FROM prior_fin.report_period)
                      = EXTRACT(DAY FROM current_fin.report_period)
                ORDER BY
                    prior_fin.announcement_date DESC,
                    prior_fin.ingested_at DESC,
                    prior_fin.source
                LIMIT 1
            ) prior_fin ON TRUE
            WHERE b.adjust = ?
              AND (
                    NOT EXISTS (
                        SELECT 1 FROM raw_asset_universe
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM raw_asset_universe universe
                        WHERE universe.symbol = b.symbol
                          AND universe.asset_type = 'stock'
                          AND universe.region = 'cn'
                          AND universe.is_active
                    )
              )
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
                WHEN current_roe_ttm IS NOT NULL
                 AND prior_year_roe_ttm IS NOT NULL
                THEN current_roe_ttm - prior_year_roe_ttm
                ELSE NULL
            END AS roe_yoy_growth,
            CASE
                WHEN bar_count_5 = 5
                 AND amount_count_5 = 5
                 AND flow_count_5 = 5
                 AND ABS(amount_sum_5) > 1e-8
                THEN flow_sum_5 / amount_sum_5
                ELSE NULL
            END AS main_inflow_5d_ratio,
            CASE
                WHEN lhb_institution_net IS NOT NULL
                 AND ABS(amount) > 1e-8
                THEN lhb_institution_net / amount
                ELSE NULL
            END AS lhb_institution_net_ratio,
            CASE
                WHEN turnover_count_20 = 20
                 AND turnover_std_20 > 1e-8
                THEN (turnover_rate - turnover_mean_20) / turnover_std_20
                WHEN turnover_count_20 = 20
                THEN 0.0
                ELSE NULL
            END AS turnover_z20,
            CASE
                WHEN hot_rank_pct >= 0
                 AND hot_rank_pct <= 100
                THEN 1.0 - hot_rank_pct / 100.0
                ELSE NULL
            END AS hot_rank_attention
        FROM rolling
        ORDER BY trade_date, symbol
    """
    scope_params = []
    if start_date is not None or end_date is not None:
        # Native windows need at most 19 preceding observations. Restrict the
        # source before expensive financial/valuation lateral joins, keeping
        # that warmup per symbol rather than loading years for a one-day refresh.
        scope = 'SELECT * FROM raw_daily_bars WHERE adjust=?'
        scope_params.append(adjust)
        if end_date is not None:
            scope += ' AND trade_date<=?'
            scope_params.append(end_date)
        if start_date is not None:
            scope += ''' QUALIFY trade_date>=? OR ROW_NUMBER() OVER (
                PARTITION BY symbol, trade_date<? ORDER BY trade_date DESC) <= 19'''
            scope_params.extend((start_date,start_date))
        sql=sql.replace('WITH valuation_dedup AS', 'WITH source_bars AS ('+scope+'), valuation_dedup AS',1)
        sql=sql.replace('FROM raw_daily_bars b','FROM source_bars b',1)
    with warehouse.connection(read_only=True) as conn:
        return conn.execute(
            sql,
            [*scope_params, adjust, valuation_max_age_days, valuation_max_age_days],
        ).fetchdf()


def _long_factor_frame(panel: pd.DataFrame, factor_codes: list[str] | None = None) -> pd.DataFrame:
    if factor_codes is None:
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
    db=None,
    factor_set_ids: tuple[str, ...] = (),
    trade_dates: tuple[date, ...] | None = None,
    frozen_factor_specs: tuple[FrozenFactorSpec, ...] | None = None,
    selected_features_only: bool = False,
) -> FactorCalculationResult:
    """Calculate and persist stock factors from point-in-time local data."""
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if valuation_max_age_days < 0:
        raise ValueError("valuation_max_age_days must not be negative")
    if frozen_factor_specs is None:
        if factor_set_ids and db is None:
            raise ValueError("frozen_factor_calculation_requires_db")
        frozen_factor_specs = resolve_frozen_factor_specs(db, factor_set_ids,
            valuation_max_age_days=valuation_max_age_days) if factor_set_ids else ()
    if selected_features_only and not frozen_factor_specs:
        raise ValueError("frozen_factor_calculation_requires_features")
    if any(spec.code in DISABLED_FACTOR_CODES for spec in frozen_factor_specs):
        reject_tail_proxy()
    warehouse.initialize()
    batch_id = calc_batch_id or f"factors-{uuid4().hex}"
    needed_native = list(dict.fromkeys(spec.code for spec in frozen_factor_specs if spec.native))
    panel = (_load_raw_factor_panel(warehouse, adjust=adjust,
             valuation_max_age_days=valuation_max_age_days,start_date=start_date,end_date=end_date)
             if not selected_features_only or needed_native
             else pd.DataFrame(columns=["symbol", "trade_date"]))
    if not panel.empty:
        panel["trade_date"] = pd.to_datetime(
            panel["trade_date"], errors="coerce"
        ).dt.date
        if start_date is not None:
            panel = panel[panel["trade_date"] >= start_date]
        if end_date is not None:
            panel = panel[panel["trade_date"] <= end_date]
        if trade_dates is not None:
            panel = panel[panel["trade_date"].isin(trade_dates)]
    versions = {
        definition.code: definition.version
        for definition in FACTOR_DEFINITIONS
    }
    native_frame = _standardize(_long_factor_frame(panel,
        needed_native if selected_features_only else None))
    native_frame["factor_version"] = native_frame["factor_code"].map(versions)
    long_frame = native_frame.iloc[0:0].copy() if selected_features_only else native_frame
    requested_frames = []
    seen = set() if selected_features_only else set(versions.items())
    if frozen_factor_specs:
        from app.services.factors.factor_executor import FactorExecutor
        for spec in frozen_factor_specs:
            code, key = spec.code, (spec.code, spec.version)
            if spec.native:
                frame = native_frame.loc[native_frame.factor_code == code].copy()
            else:
                execution_start, execution_end = start_date, end_date
                if execution_start is None or execution_end is None:
                    with warehouse.connection(read_only=True) as conn:
                        first, last = conn.execute("SELECT MIN(trade_date),MAX(trade_date) "
                            "FROM raw_daily_bars WHERE adjust=?", [adjust]).fetchone()
                    execution_start, execution_end = execution_start or first, execution_end or last
                if execution_start is None or execution_end is None:
                    raise ValueError(f"frozen_factor_no_source_data:{code}")
                outcome = FactorExecutor(warehouse).execute_panel(spec.execution_plan,
                    start_date=execution_start, end_date=execution_end)
                if outcome.read_errors or outcome.factors_long.empty:
                    raise ValueError(f"frozen_factor_execution_failed:{code}:{outcome.read_errors}")
                frame = outcome.factors_long.copy()
                frame["factor_code"] = code
                frame["eligible"] = (
                    np.isfinite(pd.to_numeric(frame.normalized_value, errors="coerce"))
                    & np.isfinite(pd.to_numeric(frame.raw_value, errors="coerce")))
            frame["factor_version"] = spec.version
            if spec.missing_policy == "impute_zero":
                imputed = frame["normalized_value"].isna() | frame["raw_value"].isna()
                frame["is_imputed"] = imputed
                frame["imputation_method"] = np.where(imputed, "frozen_policy_zero", None)
                for column in ("raw_value", "winsorized_value", "normalized_value"):
                    frame[column] = frame[column].fillna(0.0)
                frame["eligible"] = True
            if key in seen:
                # A custom built-in v1 replaces only that identical version.
                long_frame = long_frame.loc[~((long_frame.factor_code == code)
                                               & (long_frame.factor_version == spec.version))]
            seen.add(key)
            if trade_dates is not None:
                frame = frame[frame.trade_date.isin(trade_dates)]
            requested_frames.append(frame)
    if requested_frames:
        long_frame = pd.concat([long_frame, *requested_frames], ignore_index=True)
    cutoff = _utcnow_naive()
    output = long_frame.copy()
    if "is_imputed" not in output:
        output["is_imputed"] = False
    else:
        output["is_imputed"] = output["is_imputed"].fillna(False).astype(bool)
    if "imputation_method" not in output:
        output["imputation_method"] = None
    output["data_cutoff_at"] = cutoff
    output["calc_batch_id"] = batch_id
    output["created_at"] = cutoff
    rows_written = warehouse.upsert_frame("factor_values", output)
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
        code: round(
            float(eligible_by_factor.get(code, 0))
            / max(int(total_by_factor.get(code, 0)), 1),
            6,
        )
        for code in (dict.fromkeys(spec.code for spec in frozen_factor_specs)
                     if selected_features_only else versions)
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
