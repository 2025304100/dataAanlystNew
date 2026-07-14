"""Pure field adapters for factor-specific macro AkShare interfaces."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.services.factors.contracts import (
    normalize_dates,
    normalize_numbers,
    resolve_contract,
)


def normalize_bond_rate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "period": ("日期", "SOLAR_DATE"),
        "cn_10y_yield": (
            "中国国债收益率10年",
            "EMM00166466",
        ),
        "us_10y_yield": (
            "美国国债收益率10年",
            "EMG00001310",
        ),
    }
    normalized = resolve_contract(
        frame,
        api_key="bond_zh_us_rate",
        aliases=aliases,
        required=("period", "cn_10y_yield", "us_10y_yield"),
    )
    columns = [
        "indicator_key",
        "period",
        "value",
        "previous_value",
        "source",
    ]
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized["period"] = normalize_dates(normalized["period"])
    normalized = normalized.sort_values("period").reset_index(drop=True)
    parts = []
    for indicator_key in ("cn_10y_yield", "us_10y_yield"):
        values = normalize_numbers(normalized[indicator_key])
        part = pd.DataFrame(
            {
                "indicator_key": indicator_key,
                "period": normalized["period"],
                "value": values,
                "previous_value": values.shift(1),
                "source": "akshare:bond_zh_us_rate",
            }
        )
        parts.append(part)
    return pd.concat(parts, ignore_index=True).dropna(
        subset=["period", "value"]
    )[columns]


def normalize_margin_frame(
    frame: pd.DataFrame, *, market: str
) -> pd.DataFrame:
    if market not in {"sh", "sz"}:
        raise ValueError("market must be 'sh' or 'sz'")
    aliases = {
        "period": ("日期", "TRADE_DATE"),
        "value": (
            "融资融券余额",
            "融资余额",
            "MARGIN_BALANCE",
        ),
    }
    api_key = f"macro_china_market_margin_{market}"
    normalized = resolve_contract(
        frame,
        api_key=api_key,
        aliases=aliases,
        required=("period", "value"),
    )
    columns = [
        "indicator_key",
        "period",
        "value",
        "previous_value",
        "source",
    ]
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized["period"] = normalize_dates(normalized["period"])
    normalized["value"] = normalize_numbers(normalized["value"])
    normalized = normalized.sort_values("period").reset_index(drop=True)
    normalized["previous_value"] = normalized["value"].shift(1)
    normalized["indicator_key"] = f"cn_margin_{market}"
    normalized["source"] = f"akshare:{api_key}"
    return normalized.dropna(subset=["period", "value"])[columns]


@dataclass(frozen=True)
class MacroRegime:
    as_of: date | None
    regime: str
    position_multiplier: float
    available: bool
    cn_10y_change: float | None = None
    us_10y_change: float | None = None
    margin_change_ratio: float | None = None
    missing_indicators: tuple[str, ...] = ()


def _series_change(
    frame: pd.DataFrame, indicator_key: str, lookback: int
) -> float | None:
    subset = frame[frame["indicator_key"] == indicator_key].sort_values(
        "period"
    )
    if subset.empty:
        return None
    current = float(subset.iloc[-1]["value"])
    if len(subset) > lookback:
        baseline = float(subset.iloc[-lookback - 1]["value"])
    elif len(subset) > 1:
        baseline = float(subset.iloc[0]["value"])
    else:
        previous = subset.iloc[-1]["previous_value"]
        if pd.isna(previous):
            return None
        baseline = float(previous)
    return current - baseline


def _series_change_ratio(
    frame: pd.DataFrame, indicator_key: str, lookback: int
) -> float | None:
    subset = frame[frame["indicator_key"] == indicator_key].sort_values(
        "period"
    )
    if subset.empty:
        return None
    current = float(subset.iloc[-1]["value"])
    if len(subset) > lookback:
        baseline = float(subset.iloc[-lookback - 1]["value"])
    elif len(subset) > 1:
        baseline = float(subset.iloc[0]["value"])
    else:
        previous = subset.iloc[-1]["previous_value"]
        if pd.isna(previous):
            return None
        baseline = float(previous)
    if abs(baseline) <= 1e-8:
        return None
    return current / baseline - 1.0


def calculate_macro_regime(
    warehouse,
    *,
    as_of: date | None = None,
    lookback: int = 5,
) -> MacroRegime:
    """Calculate a market regime without creating a stock cross-section."""
    if lookback < 1:
        raise ValueError("lookback must be positive")
    warehouse.initialize()
    with warehouse.connection(read_only=True) as conn:
        frame = conn.execute(
            """
            WITH dedup AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY indicator_key, period
                           ORDER BY ingested_at DESC, source
                       ) AS row_num
                FROM raw_macro
                WHERE (? IS NULL OR period <= ?)
                  AND indicator_key IN (
                      'cn_10y_yield',
                      'us_10y_yield',
                      'cn_margin_sh',
                      'cn_margin_sz'
                  )
            )
            SELECT indicator_key, period, value, previous_value
            FROM dedup
            WHERE row_num = 1 AND value IS NOT NULL
            ORDER BY period, indicator_key
            """,
            [as_of, as_of],
        ).fetchdf()
    if frame.empty:
        return MacroRegime(
            as_of=as_of,
            regime="neutral",
            position_multiplier=1.0,
            available=False,
            missing_indicators=(
                "cn_10y_yield",
                "us_10y_yield",
                "cn_margin_sh",
                "cn_margin_sz",
            ),
        )
    frame["period"] = pd.to_datetime(
        frame["period"], errors="coerce"
    ).dt.date
    effective_as_of = as_of or frame["period"].max()
    cn_change = _series_change(frame, "cn_10y_yield", lookback)
    us_change = _series_change(frame, "us_10y_yield", lookback)
    margin_changes = [
        value
        for value in (
            _series_change_ratio(frame, "cn_margin_sh", lookback),
            _series_change_ratio(frame, "cn_margin_sz", lookback),
        )
        if value is not None
    ]
    margin_change = (
        sum(margin_changes) / len(margin_changes)
        if margin_changes
        else None
    )
    values = {
        "cn_10y_yield": cn_change,
        "us_10y_yield": us_change,
        "cn_margin": margin_change,
    }
    missing = tuple(key for key, value in values.items() if value is None)
    available = not missing

    if (
        (cn_change is not None and cn_change >= 0.15)
        or (us_change is not None and us_change >= 0.25)
        or (margin_change is not None and margin_change <= -0.05)
    ):
        regime, multiplier = "defensive", 0.70
    elif (
        cn_change is not None
        and cn_change <= -0.10
        and us_change is not None
        and us_change <= -0.15
        and margin_change is not None
        and margin_change >= 0.03
    ):
        regime, multiplier = "risk_on", 1.0
    elif (
        (cn_change is not None and cn_change > 0.05)
        or (us_change is not None and us_change > 0.10)
        or (margin_change is not None and margin_change < -0.02)
        or not available
    ):
        regime, multiplier = "cautious", 0.85
    else:
        regime, multiplier = "neutral", 1.0
    return MacroRegime(
        as_of=effective_as_of,
        regime=regime,
        position_multiplier=multiplier,
        available=available,
        cn_10y_change=cn_change,
        us_10y_change=us_change,
        margin_change_ratio=margin_change,
        missing_indicators=missing,
    )
