"""Pure minute-bar adapter and tail-session accumulation proxy."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, time

import pandas as pd

from app.services.factors.contracts import (
    normalize_numbers,
    resolve_contract,
)


MINIMUM_DAY_MINUTES = 180
MINIMUM_TAIL_MINUTES = 20
TAIL_START = time(14, 30)
SESSION_COMPLETE_AT = time(14, 59)


@dataclass(frozen=True)
class TailProxyMetrics:
    trade_date: date
    minute_count: int
    tail_minute_count: int
    day_amount: float
    tail_amount: float
    tail_amount_share: float
    tail_activity_ratio: float
    tail_return: float
    close_location: float
    proxy_score: float


def normalize_minute_frame(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "timestamp": ("时间", "日期时间", "datetime"),
        "close": ("收盘", "close"),
        "high": ("最高", "high"),
        "low": ("最低", "low"),
        "amount": ("成交额", "amount"),
    }
    normalized = resolve_contract(
        frame,
        api_key="stock_zh_a_hist_min_em",
        aliases=aliases,
        required=("timestamp", "close", "high", "low", "amount"),
    )
    if normalized.empty:
        return pd.DataFrame(columns=list(aliases))
    normalized["timestamp"] = pd.to_datetime(
        normalized["timestamp"], errors="coerce"
    )
    for column in ("close", "high", "low", "amount"):
        normalized[column] = normalize_numbers(normalized[column])
    return (
        normalized.dropna(
            subset=["timestamp", "close", "high", "low", "amount"]
        )
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )


def calculate_tail_proxy(
    frame: pd.DataFrame,
    *,
    trade_date: date,
    minimum_day_minutes: int = MINIMUM_DAY_MINUTES,
    minimum_tail_minutes: int = MINIMUM_TAIL_MINUTES,
) -> TailProxyMetrics | None:
    normalized = normalize_minute_frame(frame)
    if normalized.empty:
        return None
    day = normalized[
        normalized["timestamp"].dt.date == trade_date
    ].copy()
    tail = day[day["timestamp"].dt.time >= TAIL_START].copy()
    pre_tail = day[day["timestamp"].dt.time < TAIL_START].copy()
    if (
        len(day) < minimum_day_minutes
        or len(tail) < minimum_tail_minutes
        or pre_tail.empty
        or day.iloc[-1]["timestamp"].time() < SESSION_COMPLETE_AT
    ):
        return None
    day_amount = float(day["amount"].sum())
    tail_amount = float(tail["amount"].sum())
    pre_tail_average = float(pre_tail["amount"].mean())
    tail_average = float(tail["amount"].mean())
    first_tail_close = float(tail.iloc[0]["close"])
    last_close = float(tail.iloc[-1]["close"])
    day_high = float(day["high"].max())
    day_low = float(day["low"].min())
    if (
        day_amount <= 1e-8
        or pre_tail_average <= 1e-8
        or first_tail_close <= 1e-8
        or day_high - day_low <= 1e-8
    ):
        return None
    tail_amount_share = tail_amount / day_amount
    tail_activity_ratio = tail_average / pre_tail_average
    tail_return = last_close / first_tail_close - 1.0
    close_location = (last_close - day_low) / (day_high - day_low)
    close_location = max(0.0, min(1.0, close_location))
    proxy_score = (
        math.log(max(tail_activity_ratio, 1e-8))
        + 20.0 * tail_return
        + close_location
        - 0.5
    )
    if not math.isfinite(proxy_score):
        return None
    return TailProxyMetrics(
        trade_date=trade_date,
        minute_count=len(day),
        tail_minute_count=len(tail),
        day_amount=day_amount,
        tail_amount=tail_amount,
        tail_amount_share=tail_amount_share,
        tail_activity_ratio=tail_activity_ratio,
        tail_return=tail_return,
        close_location=close_location,
        proxy_score=proxy_score,
    )


__all__ = [
    "TailProxyMetrics",
    "calculate_tail_proxy",
    "normalize_minute_frame",
]
