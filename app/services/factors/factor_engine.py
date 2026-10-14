"""Vectorized, local-only factor calculation and cross-sectional scaling."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from datetime import date, datetime, timezone
from uuid import uuid4

import numpy as np
import pandas as pd

from app.services.factors.definitions import FACTOR_BY_CODE, FACTOR_DEFINITIONS, _PARAMS, _SOURCE_MAPPINGS
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
        tail_proxy_dedup AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, trade_date
                       ORDER BY ingested_at DESC, source
                   ) AS row_num
            FROM raw_tail_proxy
            WHERE proxy_score IS NOT NULL
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
                tail.proxy_score AS tail_proxy_score,
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
            LEFT JOIN tail_proxy_dedup tail
              ON tail.row_num = 1
             AND tail.symbol = b.symbol
             AND tail.trade_date = b.trade_date
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
            END AS hot_rank_attention,
            tail_proxy_score AS tail_accumulation_proxy
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
    db=None,
    factor_set_ids: tuple[str, ...] = (),
    trade_dates: tuple[date, ...] | None = None,
) -> FactorCalculationResult:
    """Calculate and persist stock factors from point-in-time local data."""
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
        if trade_dates is not None:
            panel = panel[panel["trade_date"].isin(trade_dates)]
    versions = {
        definition.code: definition.version
        for definition in FACTOR_DEFINITIONS
    }
    long_frame = _standardize(_long_factor_frame(panel))
    long_frame["factor_version"] = long_frame["factor_code"].map(versions)
    requested_frames = []
    resolved_keys = set()
    seen = {(code, version) for code, version in versions.items()}
    if factor_set_ids:
        if db is None:
            raise ValueError("frozen_factor_calculation_requires_db")
        from app.models.factor_evaluation import FactorSet
        from app.models.factor import Factor
        from app.models.factor_model import FactorVersion
        from app.services.factors.factor_compiler import FactorCompiler
        from app.services.factors.factor_executor import FactorExecutor

        for set_id in dict.fromkeys(factor_set_ids):
            factor_set = db.get(FactorSet, set_id)
            if factor_set is None or factor_set.status != "frozen":
                raise ValueError(f"factor_set_not_frozen:{set_id}")
            for member in factor_set.members:
                if member.role != "feature":
                    continue
                version = db.get(FactorVersion, member.factor_version_id)
                factor = db.get(Factor, member.factor_id)
                if (version is None or version.factor_id != member.factor_id
                        or version.version != member.factor_version or factor is None
                        or factor.code != member.factor_code):
                    raise ValueError(f"factor_version_binding_mismatch:{member.factor_code}")
                code = member.factor_code
                definition = FACTOR_BY_CODE.get(code)
                params = json.loads(version.params_json or "{}")
                source = json.loads(version.source_mapping_json or "{}")
                postprocess = json.loads(version.postprocess_json or "null")
                native = bool(
                    definition
                    and "".join(version.formula_expr.split()) == "".join(definition.formula.split())
                    and params == _PARAMS[code]
                    and source == _SOURCE_MAPPINGS[code]
                    and version.direction == definition.direction
                    and postprocess is None
                    and (code not in ("ep_ttm", "negative_pb")
                         or valuation_max_age_days == params.get("valuation_max_age_days"))
                )
                # A version number never authorizes reuse of another formula.
                # Validate even v1 metadata before reusing a built-in batch.
                key = (code, int(version.version))
                if key in resolved_keys:
                    continue
                if native:
                    frame = long_frame.loc[long_frame.factor_code == code].copy()
                else:
                    if postprocess and (
                        postprocess.get("neutralize")
                        or (postprocess.get("rank") and postprocess.get("zscore"))
                        or (postprocess.get("rank") or {}).get("method", "percentile") != "percentile"
                    ):
                        raise ValueError(f"unsupported_frozen_postprocess:{code}:v{version.version}")
                    compiled = FactorCompiler().compile(
                        formula=version.formula_expr, params=params,
                        direction=version.direction, postprocess=postprocess or {
                            "winsorize": {"method": "mad"},
                            "zscore": {"ddof": 0}, "missing_policy": member.missing_policy,
                        },
                    )
                    if not compiled.success or compiled.execution_plan is None:
                        reasons = ";".join(error.error_code for error in compiled.errors)
                        raise ValueError(f"unsupported_frozen_factor:{code}:v{version.version}:{reasons}")
                    dependencies = compiled.execution_plan.data_dependencies
                    mapped_fields = source.get("fields", []) + ([source["field"]] if source.get("field") else [])
                    mapped_tables = source.get("tables", []) + ([source["table"]] if source.get("table") else [])
                    if (not set(mapped_fields).issubset(dependencies.get("fields", []))
                            or not set(mapped_tables).issubset(dependencies.get("source_tables", []))):
                        raise ValueError(f"unsupported_frozen_source_mapping:{code}:v{version.version}")
                    if panel.empty:
                        raise ValueError(f"frozen_factor_no_source_data:{code}")
                    outcome = FactorExecutor(warehouse).execute_panel(
                        compiled.execution_plan,
                        start_date=start_date or panel.trade_date.min(),
                        end_date=end_date or panel.trade_date.max(),
                    )
                    if outcome.read_errors or outcome.factors_long.empty:
                        raise ValueError(f"frozen_factor_execution_failed:{code}:{outcome.read_errors}")
                    frame = outcome.factors_long.copy()
                    frame["factor_code"] = code
                    frame["eligible"] = (
                        np.isfinite(pd.to_numeric(frame.normalized_value, errors="coerce"))
                        & np.isfinite(pd.to_numeric(frame.raw_value, errors="coerce"))
                    )
                frame["factor_version"] = version.version
                if member.missing_policy == "impute_zero":
                    imputed = frame["normalized_value"].isna() | frame["raw_value"].isna()
                    frame["is_imputed"] = imputed
                    frame["imputation_method"] = np.where(imputed, "frozen_policy_zero", None)
                    for column in ("raw_value", "winsorized_value", "normalized_value"):
                        frame[column] = frame[column].fillna(0.0)
                    frame["eligible"] = True
                elif member.missing_policy not in ("exclude", "ignore"):
                    raise ValueError(f"unsupported_frozen_missing_policy:{code}:{member.missing_policy}")
                if key in seen:
                    # A custom definition of a built-in v1 replaces its native
                    # rows; never retain two meanings under the same identity.
                    long_frame = long_frame.loc[~((long_frame.factor_code == code)
                                                   & (long_frame.factor_version == version.version))]
                    requested_frames = [f for f in requested_frames
                                        if f.empty or not (f.factor_code.iloc[0] == code
                                                and f.factor_version.iloc[0] == version.version)]
                seen.add(key)
                resolved_keys.add(key)
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
