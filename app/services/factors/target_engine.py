"""Future-return labels aligned to the market trading calendar."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from app.services.factors.store import DEFAULT_TARGET_CODE, FactorWarehouse


#: 唯一产出的标签口径；别名到 store 侧常量，避免"指针优先常驻批次"与
#: 实际写入的 target_code 两处定义漂移。
TARGET_CODE = DEFAULT_TARGET_CODE


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
    warehouse: FactorWarehouse,
    *,
    adjust: str,
    signal_from: date | None = None,
    signal_to: date | None = None,
    target_horizon: int = 5,
    symbols: tuple[str, ...] | list[str] | None = None,
) -> pd.DataFrame:
    """按交易日历自连接出 entry=T+1 / exit=T+5 的标签面板。

    信号日窗口下推进 SQL（`signals` CTE），但 `bars`/`calendar` 保持全量：
    末端信号的出场 K 线在窗口之外，若把 bars 一起截短，新尾部会被误判成
    insufficient_future_calendar。
    """
    signal_filter = ""
    params: list[Any] = [adjust]
    if signal_from is not None:
        signal_filter += " AND b.trade_date >= ?"
        params.append(signal_from)
    if signal_to is not None:
        signal_filter += " AND b.trade_date <= ?"
        params.append(signal_to)
    if symbols is not None:
        if not symbols:
            return pd.DataFrame()
        signal_filter += " AND b.symbol IN (" + ",".join("?" for _ in symbols) + ")"
        params.extend(symbols)

    sql = f"""
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
            WHERE TRUE {signal_filter}
        )
        SELECT
            s.symbol,
            s.trade_date AS signal_date,
            s.close AS signal_close,
            entry_calendar.trade_date AS entry_date,
            entry_bar.trade_date AS entry_bar_date,
            entry_bar.open AS entry_open,
            entry_bar.high AS entry_high,
            entry_bar.low AS entry_low,
            entry_bar.volume AS entry_volume,
            entry_bar.amount AS entry_amount,
            previous_exit_bar.close AS previous_exit_close,
            previous_exit_bar.trade_date AS previous_exit_bar_date,
            exit_calendar.trade_date AS exit_date,
            exit_bar.trade_date AS exit_bar_date,
            exit_bar.close AS exit_close,
            exit_bar.high AS exit_high,
            exit_bar.low AS exit_low,
            exit_bar.volume AS exit_volume,
            exit_bar.amount AS exit_amount
        FROM signals s
        LEFT JOIN calendar entry_calendar
          ON entry_calendar.trade_index = s.trade_index + 1
        LEFT JOIN calendar previous_exit_calendar
          ON previous_exit_calendar.trade_index = s.trade_index + {target_horizon - 1}
        LEFT JOIN calendar exit_calendar
          ON exit_calendar.trade_index = s.trade_index + {target_horizon}
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
        return conn.execute(sql, params).fetchdf()


def _invalid_reason(row: dict, *, limit_threshold: float) -> str | None:
    if row["entry_date"] is None or pd.isna(row["entry_date"]):
        return "insufficient_future_calendar"
    if row["exit_date"] is None or pd.isna(row["exit_date"]):
        return "insufficient_future_calendar"
    # Calendar presence and numeric validity are different facts. A present
    # bar with NaN/Inf must not be mistaken for suspension (or yield -100%).
    entry_bar_date = row.get("entry_bar_date", row["entry_open"])
    exit_bar_date = row.get("exit_bar_date", row["exit_close"])
    if entry_bar_date is None or pd.isna(entry_bar_date):
        return "missing_entry_bar"
    if exit_bar_date is None or pd.isna(exit_bar_date):
        return "missing_exit_bar"

    def finite_values(fields: tuple[str, ...]) -> list[float] | None:
        try:
            values = [float(row[field]) for field in fields]
        except (TypeError, ValueError):
            return None
        return values if all(isfinite(value) for value in values) else None

    entry_values = finite_values(("entry_open", "entry_high", "entry_low",
        "entry_volume", "entry_amount", "signal_close"))
    if entry_values is None:
        return "entry_non_finite_values"
    if any(value <= 0 for value in entry_values):
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
    exit_fields = ("exit_close", "exit_high", "exit_low", "exit_volume", "exit_amount")
    previous_bar_date = row.get("previous_exit_bar_date", row["previous_exit_close"])
    if previous_bar_date is not None and not pd.isna(previous_bar_date):
        exit_fields += ("previous_exit_close",)
    exit_values = finite_values(exit_fields)
    if exit_values is None:
        return "exit_non_finite_values"
    if any(value <= 0 for value in exit_values):
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


def preview_targets(
    warehouse: FactorWarehouse,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    calc_batch_id: str | None = None,
    adjust: str = "qfq",
    limit_threshold: float = 0.095,
    target_horizon: int = 5,
    symbols: tuple[str, ...] | list[str] | None = None,
    as_of_exit_date: date | None = None,
) -> pd.DataFrame:
    """Read-only T+1 open to T+N close labels using the production label rules.

    No initialization, upsert or coverage refresh is allowed here. Preflight can
    therefore inspect a first-use horizon without requiring a persisted batch.
    The complete qfq calendar remains the entry/exit clock even for a small pool.
    """
    if not isinstance(target_horizon, int) or not 1 <= target_horizon <= 20:
        raise ValueError("target_horizon must be an integer between 1 and 20")
    target_code = f"target_{target_horizon}d_return"
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if not 0 < limit_threshold < 1:
        raise ValueError("limit_threshold must be between 0 and 1")
    # 窗口下推进 SQL 是为了"只重算尾部几天"真的省钱；下面的 pandas 过滤仍保留，
    # 因为 panel 也可能来自调用方注入的桩，正确性不能只靠 SQL 那一层。
    panel = _load_target_panel(
        warehouse,
        adjust=adjust,
        signal_from=start_date,
        signal_to=end_date,
        **({"target_horizon": target_horizon} if target_horizon != 5 else {}),
        **({"symbols": symbols} if symbols is not None else {}),
    )
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
        records.append(
            {
                "symbol": row["symbol"],
                "signal_date": row["signal_date"],
                "entry_date": row["entry_date"],
                "exit_date": row["exit_date"],
                "target_code": target_code,
                "target_value": target_value,
                "is_tradable": is_tradable,
                "invalid_reason": reason,
                "calc_batch_id": batch_id,
                "created_at": created_at,
            }
        )
    frame = pd.DataFrame.from_records(records, columns=["symbol", "signal_date", "entry_date",
        "exit_date", "target_code", "target_value", "is_tradable", "invalid_reason",
        "calc_batch_id", "created_at"])
    if as_of_exit_date is not None and not frame.empty:
        exits = pd.to_datetime(frame["exit_date"], errors="coerce")
        frame = frame[exits.notna() & (exits <= pd.Timestamp(as_of_exit_date))]
    return frame.reset_index(drop=True)


def calculate_targets(
    warehouse: FactorWarehouse,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    calc_batch_id: str | None = None,
    adjust: str = "qfq",
    limit_threshold: float = 0.095,
    target_horizon: int = 5,
    symbols: tuple[str, ...] | list[str] | None = None,
) -> TargetCalculationResult:
    """Persist the same labels that read-only preflight previews."""
    warehouse.initialize()
    batch_id = calc_batch_id or f"targets-{uuid4().hex}"
    target_code = f"target_{target_horizon}d_return"
    frame = preview_targets(warehouse, start_date=start_date, end_date=end_date,
        calc_batch_id=batch_id, adjust=adjust, limit_threshold=limit_threshold,
        target_horizon=target_horizon, symbols=symbols)
    rows_written = warehouse.upsert_frame("factor_targets", frame)
    # 写完就刷新覆盖表：覆盖 gate 的依据必须是这次写入后的真实状态，
    # 而不是调用方再去扫一遍全量标签行。
    warehouse.refresh_label_coverage(batch_id, target_code, adjust=adjust)
    return TargetCalculationResult(
        calc_batch_id=batch_id,
        rows_written=rows_written,
        tradable_rows=int(frame["is_tradable"].sum()) if not frame.empty else 0,
        invalid_rows=int((~frame["is_tradable"]).sum()) if not frame.empty else 0,
        signal_date_count=int(frame["signal_date"].nunique())
        if not frame.empty
        else 0,
        symbol_count=int(frame["symbol"].nunique())
        if not frame.empty
        else 0,
    )
