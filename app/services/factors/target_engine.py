"""Future-return labels aligned to the market trading calendar."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

import numpy as np
import pandas as pd

from app.services.factors.store import FactorWarehouse


TARGET_CODE = "target_5d_return"


@dataclass(frozen=True)
class TargetCalculationResult:
    calc_batch_id: str
    rows_written: int
    tradable_rows: int
    invalid_rows: int
    signal_date_count: int
    symbol_count: int


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_target_panel(
    warehouse: FactorWarehouse, *, adjust: str
) -> pd.DataFrame:
    sql = """
        WITH bars AS (
            SELECT
                symbol, trade_date, open, high, low, close, volume, amount
            FROM raw_daily_bars
            WHERE adjust = ?
        ),
        calendar AS (
            SELECT
                trade_date,
                ROW_NUMBER() OVER (ORDER BY trade_date) AS trade_index
            FROM (SELECT DISTINCT trade_date FROM bars)
        ),
        signals AS (
            SELECT b.*, c.trade_index
            FROM bars b
            JOIN calendar c USING (trade_date)
        )
        SELECT
            s.symbol,
            s.trade_date AS signal_date,
            s.close AS signal_close,
            entry_calendar.trade_date AS entry_date,
            entry_bar.open AS entry_open,
            entry_bar.high AS entry_high,
            entry_bar.low AS entry_low,
            entry_bar.volume AS entry_volume,
            entry_bar.amount AS entry_amount,
            previous_exit_bar.close AS previous_exit_close,
            exit_calendar.trade_date AS exit_date,
            exit_bar.close AS exit_close,
            exit_bar.high AS exit_high,
            exit_bar.low AS exit_low,
            exit_bar.volume AS exit_volume,
            exit_bar.amount AS exit_amount
        FROM signals s
        LEFT JOIN calendar entry_calendar
          ON entry_calendar.trade_index = s.trade_index + 1
        LEFT JOIN calendar previous_exit_calendar
          ON previous_exit_calendar.trade_index = s.trade_index + 4
        LEFT JOIN calendar exit_calendar
          ON exit_calendar.trade_index = s.trade_index + 5
        LEFT JOIN bars entry_bar
          ON entry_bar.symbol = s.symbol
         AND entry_bar.trade_date = entry_calendar.trade_date
        LEFT JOIN bars previous_exit_bar
          ON previous_exit_bar.symbol = s.symbol
         AND previous_exit_bar.trade_date = previous_exit_calendar.trade_date
        LEFT JOIN bars exit_bar
          ON exit_bar.symbol = s.symbol
         AND exit_bar.trade_date = exit_calendar.trade_date
        ORDER BY s.trade_date, s.symbol
    """
    with warehouse.connection(read_only=True) as conn:
        return conn.execute(sql, [adjust]).fetchdf()


def _invalid_reason(row: dict, *, limit_threshold: float) -> str | None:
    if row["entry_date"] is None or pd.isna(row["entry_date"]):
        return "insufficient_future_calendar"
    if row["exit_date"] is None or pd.isna(row["exit_date"]):
        return "insufficient_future_calendar"
    if row["entry_open"] is None or pd.isna(row["entry_open"]):
        return "missing_entry_bar"
    if row["exit_close"] is None or pd.isna(row["exit_close"]):
        return "missing_exit_bar"
    if (
        float(row["entry_open"]) <= 0
        or row["entry_volume"] is None
        or pd.isna(row["entry_volume"])
        or float(row["entry_volume"]) <= 0
        or row["entry_amount"] is None
        or pd.isna(row["entry_amount"])
        or float(row["entry_amount"]) <= 0
    ):
        return "entry_not_tradable"
    signal_close = row["signal_close"]
    if (
        signal_close is not None
        and not pd.isna(signal_close)
        and float(signal_close) > 0
        and row["entry_high"] is not None
        and row["entry_low"] is not None
        and not pd.isna(row["entry_high"])
        and not pd.isna(row["entry_low"])
        and abs(float(row["entry_high"]) - float(row["entry_low"])) <= 1e-8
        and float(row["entry_open"]) / float(signal_close) - 1
        >= limit_threshold
    ):
        return "entry_locked_limit_up"
    if (
        float(row["exit_close"]) <= 0
        or row["exit_volume"] is None
        or pd.isna(row["exit_volume"])
        or float(row["exit_volume"]) <= 0
        or row["exit_amount"] is None
        or pd.isna(row["exit_amount"])
        or float(row["exit_amount"]) <= 0
    ):
        return "exit_not_tradable"
    previous_close = row["previous_exit_close"]
    if (
        previous_close is not None
        and not pd.isna(previous_close)
        and float(previous_close) > 0
        and row["exit_high"] is not None
        and row["exit_low"] is not None
        and not pd.isna(row["exit_high"])
        and not pd.isna(row["exit_low"])
        and abs(float(row["exit_high"]) - float(row["exit_low"])) <= 1e-8
        and float(row["exit_close"]) / float(previous_close) - 1
        <= -limit_threshold
    ):
        return "exit_locked_limit_down"
    return None


def calculate_targets(
    warehouse: FactorWarehouse,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    calc_batch_id: str | None = None,
    adjust: str = "qfq",
    limit_threshold: float = 0.095,
) -> TargetCalculationResult:
    """Persist T+1 open to T+5 close labels without skipping suspensions."""
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if not 0 < limit_threshold < 1:
        raise ValueError("limit_threshold must be between 0 and 1")
    warehouse.initialize()
    panel = _load_target_panel(warehouse, adjust=adjust)
    if not panel.empty:
        for column in ("signal_date", "entry_date", "exit_date"):
            panel[column] = pd.to_datetime(
                panel[column], errors="coerce"
            ).dt.date
        if start_date is not None:
            panel = panel[panel["signal_date"] >= start_date]
        if end_date is not None:
            panel = panel[panel["signal_date"] <= end_date]
    batch_id = calc_batch_id or f"targets-{uuid4().hex}"
    created_at = _utcnow_naive()
    records = []
    tradable_rows = 0
    for row in panel.to_dict("records"):
        reason = _invalid_reason(row, limit_threshold=limit_threshold)
        is_tradable = reason is None
        target_value = None
        if is_tradable:
            target_value = float(row["exit_close"]) / float(
                row["entry_open"]
            ) - 1.0
            if not np.isfinite(target_value):
                reason = "non_finite_return"
                is_tradable = False
                target_value = None
        if is_tradable:
            tradable_rows += 1
        records.append(
            {
                "symbol": row["symbol"],
                "signal_date": row["signal_date"],
                "entry_date": row["entry_date"],
                "exit_date": row["exit_date"],
                "target_code": TARGET_CODE,
                "target_value": target_value,
                "is_tradable": is_tradable,
                "invalid_reason": reason,
                "calc_batch_id": batch_id,
                "created_at": created_at,
            }
        )
    rows_written = warehouse.upsert_frame(
        "factor_targets", pd.DataFrame.from_records(records)
    )
    return TargetCalculationResult(
        calc_batch_id=batch_id,
        rows_written=rows_written,
        tradable_rows=tradable_rows,
        invalid_rows=len(records) - tradable_rows,
        signal_date_count=int(panel["signal_date"].nunique())
        if not panel.empty
        else 0,
        symbol_count=int(panel["symbol"].nunique())
        if not panel.empty
        else 0,
    )
