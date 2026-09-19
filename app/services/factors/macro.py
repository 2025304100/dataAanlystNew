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
    market_amount_change_ratio: float | None = None
    market_amount_z20: float | None = None
    advancing_ratio: float | None = None
    margin_amount_divergence: float | None = None
    liquidity_score: float | None = None
    liquidity_available: bool = False
    missing_indicators: tuple[str, ...] = ()
    missing_liquidity_indicators: tuple[str, ...] = ()


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


def _load_market_liquidity(
    warehouse,
    *,
    as_of: date | None,
    adjust: str,
) -> pd.DataFrame:
    with warehouse.connection(read_only=True) as conn:
        frame = conn.execute(
            """
            WITH stock_bars AS (
                SELECT
                    b.symbol,
                    b.trade_date,
                    b.close,
                    b.amount,
                    LAG(b.close) OVER (
                        PARTITION BY b.symbol ORDER BY b.trade_date
                    ) AS previous_close
                FROM raw_daily_bars b
                INNER JOIN raw_asset_universe u
                    ON u.symbol = b.symbol
                WHERE b.adjust = ?
                  AND LOWER(u.asset_type) = 'stock'
                  AND LOWER(COALESCE(u.region, '')) = 'cn'
                  AND (? IS NULL OR b.trade_date <= ?)
            )
            SELECT
                trade_date,
                SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END)
                    AS market_amount,
                AVG(
                    CASE
                        WHEN previous_close IS NULL OR close IS NULL THEN NULL
                        WHEN close > previous_close THEN 1.0
                        ELSE 0.0
                    END
                ) AS advancing_ratio,
                COUNT(*) AS symbol_count
            FROM stock_bars
            GROUP BY trade_date
            ORDER BY trade_date
            """,
            [adjust, as_of, as_of],
        ).fetchdf()
    if not frame.empty:
        frame["trade_date"] = pd.to_datetime(
            frame["trade_date"], errors="coerce"
        ).dt.date
    return frame


def _market_change_ratio(frame: pd.DataFrame, lookback: int) -> float | None:
    if frame.empty:
        return None
    values = pd.to_numeric(frame["market_amount"], errors="coerce").dropna()
    if len(values) < 2:
        return None
    baseline = values.iloc[-lookback - 1] if len(values) > lookback else values.iloc[0]
    if abs(float(baseline)) <= 1e-8:
        return None
    return float(values.iloc[-1]) / float(baseline) - 1.0


def _market_amount_zscore(
    frame: pd.DataFrame,
    *,
    window: int = 20,
    minimum_history: int = 5,
) -> float | None:
    if frame.empty:
        return None
    values = pd.to_numeric(frame["market_amount"], errors="coerce").dropna()
    if len(values) <= minimum_history:
        return None
    current = float(values.iloc[-1])
    history = values.iloc[max(0, len(values) - window - 1):-1]
    if len(history) < minimum_history:
        return None
    std = float(history.std(ddof=0))
    if not pd.notna(std) or std <= 1e-8:
        return 0.0
    return (current - float(history.mean())) / std


def _calculate_liquidity_score(
    *,
    margin_change: float | None,
    amount_change: float | None,
    amount_z20: float | None,
    advancing_ratio: float | None,
    divergence: float | None,
) -> float | None:
    components: list[tuple[float, float]] = []
    if amount_z20 is not None:
        components.append((max(0.0, min(100.0, 50 + amount_z20 * 15)), 0.30))
    if advancing_ratio is not None:
        components.append((max(0.0, min(100.0, advancing_ratio * 100)), 0.35))
    if margin_change is not None:
        components.append((max(0.0, min(100.0, 50 + margin_change * 500)), 0.25))
    if amount_change is not None:
        components.append((max(0.0, min(100.0, 50 + amount_change * 250)), 0.10))
    if not components:
        return None
    weighted = sum(value * weight for value, weight in components)
    total_weight = sum(weight for _, weight in components)
    score = weighted / total_weight
    if divergence is not None and divergence > 0.08:
        score -= min(15.0, (divergence - 0.08) * 100)
    return round(max(0.0, min(100.0, score)), 2)


def calculate_macro_regime(
    warehouse,
    *,
    as_of: date | None = None,
    lookback: int = 5,
    adjust: str = "qfq",
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
    liquidity_frame = _load_market_liquidity(
        warehouse,
        as_of=as_of,
        adjust=adjust,
    )
    if frame.empty and liquidity_frame.empty:
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
    if not frame.empty:
        frame["period"] = pd.to_datetime(
            frame["period"], errors="coerce"
        ).dt.date
    effective_dates = []
    if not frame.empty:
        effective_dates.append(frame["period"].max())
    if not liquidity_frame.empty:
        effective_dates.append(liquidity_frame["trade_date"].max())
    effective_as_of = as_of or max(effective_dates)
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
    amount_change = _market_change_ratio(liquidity_frame, lookback)
    amount_z20 = _market_amount_zscore(liquidity_frame)
    advancing_ratio = (
        float(liquidity_frame.iloc[-1]["advancing_ratio"])
        if not liquidity_frame.empty
        and pd.notna(liquidity_frame.iloc[-1]["advancing_ratio"])
        else None
    )
    divergence = (
        margin_change - amount_change
        if margin_change is not None and amount_change is not None
        else None
    )
    liquidity_values = {
        "market_amount_change": amount_change,
        "market_amount_z20": amount_z20,
        "advancing_ratio": advancing_ratio,
    }
    missing_liquidity = tuple(
        key for key, value in liquidity_values.items() if value is None
    )
    liquidity_available = not missing_liquidity
    liquidity_score = _calculate_liquidity_score(
        margin_change=margin_change,
        amount_change=amount_change,
        amount_z20=amount_z20,
        advancing_ratio=advancing_ratio,
        divergence=divergence,
    )
    values = {
        "cn_10y_yield": cn_change,
        "us_10y_yield": us_change,
        "cn_margin": margin_change,
    }
    missing = tuple(key for key, value in values.items() if value is None)
    available = not missing

    liquidity_defensive = (
        liquidity_score is not None and liquidity_score <= 30
    ) or (
        amount_z20 is not None
        and amount_z20 <= -1.5
        and advancing_ratio is not None
        and advancing_ratio <= 0.35
    )
    liquidity_cautious = (
        liquidity_score is not None and liquidity_score < 45
    ) or (divergence is not None and divergence > 0.08)

    if (
        (cn_change is not None and cn_change >= 0.15)
        or (us_change is not None and us_change >= 0.25)
        or (margin_change is not None and margin_change <= -0.05)
        or liquidity_defensive
    ):
        regime, multiplier = "defensive", 0.70
    elif (
        cn_change is not None
        and cn_change <= -0.10
        and us_change is not None
        and us_change <= -0.15
        and margin_change is not None
        and margin_change >= 0.03
        and (liquidity_score is None or liquidity_score >= 60)
    ):
        regime, multiplier = "risk_on", 1.0
    elif (
        (cn_change is not None and cn_change > 0.05)
        or (us_change is not None and us_change > 0.10)
        or (margin_change is not None and margin_change < -0.02)
        or liquidity_cautious
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
        market_amount_change_ratio=amount_change,
        market_amount_z20=amount_z20,
        advancing_ratio=advancing_ratio,
        margin_amount_divergence=divergence,
        liquidity_score=liquidity_score,
        liquidity_available=liquidity_available,
        missing_indicators=missing,
        missing_liquidity_indicators=missing_liquidity,
    )
