"""VectorBT research adapter backed by the project's local SQL data.

This module complements the event-driven production backtester. It translates
project prices and Score snapshots into matrix signals and delegates portfolio
simulation to vectorbt.Portfolio.from_signals.
"""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.symbol import Symbol


TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.03
SIGNAL_MODES = {"ma_cross", "score_trend"}


class VectorBTUnavailable(RuntimeError):
    """Raised when the optional VectorBT runtime cannot be imported."""


@dataclass(frozen=True)
class VectorBTConfig:
    signal_mode: str = "score_trend"
    initial_cash: float = 1_000_000.0
    fast_window: int = 10
    slow_window: int = 20
    quality_min: float = 60.0
    timing_min: float = 55.0
    quality_exit: float = 45.0
    timing_exit: float = 40.0
    position_pct: float = 0.1
    max_positions: int = 10
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.001
    slippage_rate: float = 0.001
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    execution_lag: int = 1
    disable_numba: bool = True

    def validate(self) -> None:
        if self.signal_mode not in SIGNAL_MODES:
            raise ValueError(
                f"signal_mode must be one of {sorted(SIGNAL_MODES)}"
            )
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if not 1 <= self.fast_window < self.slow_window <= 500:
            raise ValueError(
                "windows must satisfy 1 <= fast_window < slow_window <= 500"
            )
        if not 0 < self.position_pct <= 1:
            raise ValueError("position_pct must be between 0 and 1")
        if not 1 <= self.max_positions <= 100:
            raise ValueError("max_positions must be between 1 and 100")
        if not 0 <= self.execution_lag <= 5:
            raise ValueError("execution_lag must be between 0 and 5")
        for name in (
            "commission_rate",
            "stamp_tax_rate",
            "slippage_rate",
        ):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.min_commission < 0:
            raise ValueError("min_commission must not be negative")
        for name in ("stop_loss_pct", "take_profit_pct"):
            value = getattr(self, name)
            if value is not None and not 0 < value < 1:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True)
class VectorBTInputFrames:
    close: pd.DataFrame
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    quality: pd.DataFrame
    timing: pd.DataFrame
    ranking: pd.DataFrame
    symbol_ids: dict[str, int]
    score_weight_mode: str
    factor_model_run_id: str | None


def _load_vectorbt(*, disable_numba: bool):
    # Numba 0.66 can spend minutes compiling during first import on Windows.
    # The default prioritizes predictable local execution. Production Linux
    # users can opt into JIT through the CLI.
    if disable_numba:
        os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
    try:
        import vectorbt as vbt
    except ImportError as exc:
        raise VectorBTUnavailable(
            "VectorBT is not installed; run pip install vectorbt==0.28.5"
        ) from exc
    return vbt


def _empty_score_frame(close: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(np.nan, index=close.index, columns=close.columns)


def _pivot_prices(
    frame: pd.DataFrame,
    field: str,
    *,
    columns: list[str],
) -> pd.DataFrame:
    result = frame.pivot_table(
        index="trade_date",
        columns="symbol",
        values=field,
        aggfunc="last",
    )
    return result.reindex(columns=columns).sort_index().astype(float)


def load_project_frames(
    db: Session,
    *,
    symbol_ids: list[int],
    start_date: date,
    end_date: date,
    score_weight_mode: str = "manual",
    factor_model_run_id: str | None = None,
) -> VectorBTInputFrames:
    """Load aligned price and Score matrices from the local business DB."""
    if not symbol_ids:
        raise ValueError("symbol_ids must not be empty")
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if score_weight_mode not in {"manual", "ridge"}:
        raise ValueError("score_weight_mode must be manual or ridge")
    if score_weight_mode == "ridge" and not factor_model_run_id:
        raise ValueError("factor_model_run_id is required for ridge mode")

    symbols = db.execute(
        select(Symbol)
        .where(Symbol.id.in_(symbol_ids))
        .order_by(Symbol.id)
    ).scalars().all()
    if len(symbols) != len(set(symbol_ids)):
        found = {item.id for item in symbols}
        missing = sorted(set(symbol_ids) - found)
        raise ValueError(f"symbols not found: {missing}")
    symbol_map = {item.id: item.symbol for item in symbols}
    ordered_codes = [symbol_map[item] for item in symbol_ids]

    bar_rows = db.execute(
        select(
            DailyBar.symbol_id,
            DailyBar.trade_date,
            DailyBar.open,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
        )
        .where(
            DailyBar.symbol_id.in_(symbol_ids),
            DailyBar.trade_date >= start_date,
            DailyBar.trade_date <= end_date,
        )
        .order_by(DailyBar.trade_date, DailyBar.symbol_id)
    ).all()
    if not bar_rows:
        raise ValueError("no daily bars found in the requested range")
    bars = pd.DataFrame(
        bar_rows,
        columns=[
            "symbol_id",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
        ],
    )
    bars["symbol"] = bars["symbol_id"].map(symbol_map)
    close = _pivot_prices(bars, "close", columns=ordered_codes)
    open_price = _pivot_prices(bars, "open", columns=ordered_codes)
    high = _pivot_prices(bars, "high", columns=ordered_codes)
    low = _pivot_prices(bars, "low", columns=ordered_codes)

    score_stmt = select(Score).where(
        Score.symbol_id.in_(symbol_ids),
        Score.trade_date >= start_date,
        Score.trade_date <= end_date,
        Score.weight_mode == score_weight_mode,
    )
    if score_weight_mode == "ridge":
        score_stmt = score_stmt.where(
            Score.factor_model_run_id == factor_model_run_id
        )
    scores = db.execute(
        score_stmt.order_by(Score.trade_date, Score.symbol_id, Score.id)
    ).scalars().all()
    if not scores:
        quality = _empty_score_frame(close)
        timing = _empty_score_frame(close)
        ranking = _empty_score_frame(close)
    else:
        score_rows = []
        for item in scores:
            quality_value = (
                item.factor_quality_score
                if score_weight_mode == "ridge"
                and item.factor_quality_score is not None
                else item.quality_score
            )
            timing_value = (
                item.factor_timing_score
                if score_weight_mode == "ridge"
                and item.factor_timing_score is not None
                else item.timing_score
            )
            ranking_value = (
                item.model_alpha_score
                if score_weight_mode == "ridge"
                else item.priority_score
            )
            score_rows.append(
                {
                    "symbol": symbol_map[item.symbol_id],
                    "trade_date": item.trade_date,
                    "quality": quality_value,
                    "timing": timing_value,
                    "ranking": ranking_value,
                    "id": item.id,
                }
            )
        score_frame = (
            pd.DataFrame(score_rows)
            .sort_values("id")
            .drop_duplicates(["symbol", "trade_date"], keep="last")
        )
        quality = _pivot_prices(
            score_frame, "quality", columns=ordered_codes
        ).reindex(close.index).ffill()
        timing = _pivot_prices(
            score_frame, "timing", columns=ordered_codes
        ).reindex(close.index).ffill()
        ranking = _pivot_prices(
            score_frame, "ranking", columns=ordered_codes
        ).reindex(close.index).ffill()

    return VectorBTInputFrames(
        close=close,
        open=open_price,
        high=high,
        low=low,
        quality=quality,
        timing=timing,
        ranking=ranking,
        symbol_ids={code: symbol_id for symbol_id, code in symbol_map.items()},
        score_weight_mode=score_weight_mode,
        factor_model_run_id=factor_model_run_id,
    )


def _select_daily_top_n(
    eligible: pd.DataFrame,
    ranking: pd.DataFrame,
    limit: int,
) -> pd.DataFrame:
    """Select deterministic daily Top-N candidates by descending score."""
    ordered_columns = sorted(eligible.columns)
    eligible_scores = ranking.reindex(
        index=eligible.index,
        columns=ordered_columns,
    ).where(eligible.reindex(columns=ordered_columns))
    ranks = eligible_scores.rank(
        axis=1,
        method="first",
        ascending=False,
        na_option="bottom",
    )
    selected = ranks.le(limit) & eligible_scores.notna()
    return selected.reindex(columns=eligible.columns).fillna(False)


def build_vectorbt_signals(
    frames: VectorBTInputFrames,
    config: VectorBTConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build entry/exit matrices without look-ahead data."""
    config.validate()
    close = frames.close
    fast_ma = close.rolling(config.fast_window).mean()
    slow_ma = close.rolling(config.slow_window).mean()
    trend_up = fast_ma > slow_ma
    trend_down = fast_ma < slow_ma

    if config.signal_mode == "ma_cross":
        entry_state = trend_up
        exit_state = trend_down
    else:
        if frames.quality.notna().sum().sum() == 0:
            raise ValueError(
                "score_trend mode requires historical Score coverage"
            )
        if frames.ranking.notna().sum().sum() == 0:
            raise ValueError(
                "score_trend mode requires ranking Score coverage"
            )
        eligible_entry_state = (
            (frames.quality >= config.quality_min)
            & (frames.timing >= config.timing_min)
            & trend_up
        )
        entry_state = _select_daily_top_n(
            eligible_entry_state,
            frames.ranking,
            config.max_positions,
        )
        exit_state = (
            (frames.quality < config.quality_exit)
            | (frames.timing < config.timing_exit)
            | trend_down
        )

    previous_entry_state = entry_state.shift(1, fill_value=False)
    previous_exit_state = exit_state.shift(1, fill_value=False)
    entries = (entry_state & ~previous_entry_state).fillna(False)
    exits = (exit_state & ~previous_exit_state & ~entries).fillna(False)
    if config.execution_lag:
        entries = entries.shift(
            config.execution_lag, fill_value=False
        )
        exits = exits.shift(
            config.execution_lag, fill_value=False
        )
    tradable = close.notna()
    return entries & tradable, exits & tradable


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return [
        {key: _json_safe(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _sharpe_ratio(equity: pd.Series) -> float:
    returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if returns.empty:
        return 0.0
    std = float(returns.std(ddof=0))
    if std <= 0 or not math.isfinite(std):
        return 0.0
    annual_return = float(returns.mean()) * TRADING_DAYS_PER_YEAR
    return (
        annual_return - RISK_FREE_RATE
    ) / (std * math.sqrt(TRADING_DAYS_PER_YEAR))


def _equal_weight_benchmark(close: pd.DataFrame) -> float:
    returns: list[float] = []
    for column in close.columns:
        series = close[column].dropna()
        if len(series) < 2 or float(series.iloc[0]) <= 0:
            continue
        returns.append(float(series.iloc[-1] / series.iloc[0] - 1))
    return sum(returns) / len(returns) if returns else 0.0


def run_vectorbt_backtest(
    frames: VectorBTInputFrames,
    config: VectorBTConfig,
) -> dict[str, Any]:
    """Run a cash-sharing long-only VectorBT portfolio."""
    config.validate()
    vbt = _load_vectorbt(disable_numba=config.disable_numba)
    counts = frames.close.notna().sum()
    active_columns = [
        column
        for column in frames.close.columns
        if int(counts.get(column, 0)) >= config.slow_window + 2
    ]
    if not active_columns:
        raise ValueError(
            "not enough price history for the configured slow_window"
        )

    active_frames = VectorBTInputFrames(
        close=frames.close.loc[:, active_columns],
        open=frames.open.loc[:, active_columns],
        high=frames.high.loc[:, active_columns],
        low=frames.low.loc[:, active_columns],
        quality=frames.quality.loc[:, active_columns],
        timing=frames.timing.loc[:, active_columns],
        ranking=frames.ranking.loc[:, active_columns],
        symbol_ids={
            code: frames.symbol_ids[code] for code in active_columns
        },
        score_weight_mode=frames.score_weight_mode,
        factor_model_run_id=frames.factor_model_run_id,
    )
    entries, exits = build_vectorbt_signals(active_frames, config)
    close = active_frames.close.dropna(how="all")
    entries = entries.reindex(close.index).fillna(False)
    exits = exits.reindex(close.index).fillna(False)
    simulation_close = close.ffill().bfill()

    # VectorBT from_signals applies one fee rate to both sides. Splitting the
    # sell-only stamp tax equally preserves the proportional round-trip total:
    # buy commission + sell commission + sell stamp tax.
    equivalent_fee_rate = (
        config.commission_rate + config.stamp_tax_rate / 2.0
    )
    effective_position_pct = min(
        config.position_pct, 1.0 / config.max_positions
    )
    portfolio_kwargs: dict[str, Any] = {
        "close": simulation_close,
        "entries": entries,
        "exits": exits,
        "size": effective_position_pct,
        "size_type": "percent",
        "init_cash": config.initial_cash,
        "cash_sharing": True,
        "group_by": True,
        "call_seq": "auto",
        "fees": equivalent_fee_rate,
        "slippage": config.slippage_rate,
        "freq": "1D",
    }
    if config.stop_loss_pct is not None:
        portfolio_kwargs["sl_stop"] = config.stop_loss_pct
    if config.take_profit_pct is not None:
        portfolio_kwargs["tp_stop"] = config.take_profit_pct

    portfolio = vbt.Portfolio.from_signals(**portfolio_kwargs)
    equity = portfolio.value(group_by=True)
    if isinstance(equity, pd.DataFrame):
        equity = equity.iloc[:, 0]
    equity = equity.astype(float)
    final_value = _finite_number(equity.iloc[-1], config.initial_cash)
    total_return = _finite_number(
        portfolio.total_return(group_by=True)
    )
    max_drawdown = abs(
        _finite_number(portfolio.max_drawdown(group_by=True))
    )
    trade_count = int(
        _finite_number(portfolio.trades.count(group_by=True))
    )
    win_rate = (
        _finite_number(portfolio.trades.win_rate(group_by=True))
        if trade_count
        else 0.0
    )
    profit_factor = (
        _optional_number(portfolio.trades.profit_factor(group_by=True))
        if trade_count
        else 0.0
    )
    benchmark_return = _equal_weight_benchmark(close)
    trade_records = _records(portfolio.trades.records_readable)
    equity_curve = [
        {
            "date": _json_safe(index),
            "equity": round(_finite_number(value), 2),
        }
        for index, value in equity.items()
    ]

    return {
        "engine": {
            "name": "vectorbt",
            "version": str(vbt.__version__),
            "pandas_version": str(pd.__version__),
            "numpy_version": str(np.__version__),
            "numba_disabled": config.disable_numba,
        },
        "range": {
            "start_date": _json_safe(close.index.min()),
            "end_date": _json_safe(close.index.max()),
            "trading_days": int(len(close.index)),
        },
        "symbols": active_columns,
        "score_weight_mode": frames.score_weight_mode,
        "factor_model_run_id": frames.factor_model_run_id,
        "config": asdict(config),
        "signals": {
            "entries": int(entries.to_numpy(dtype=bool).sum()),
            "exits": int(exits.to_numpy(dtype=bool).sum()),
        },
        "selection": {
            "mode": (
                "daily_top_n"
                if config.signal_mode == "score_trend"
                else "none"
            ),
            "ranking_field": (
                "model_alpha_score"
                if frames.score_weight_mode == "ridge"
                else "priority_score"
            ),
            "top_n": config.max_positions,
        },
        "metrics": {
            "initial_cash": round(config.initial_cash, 2),
            "final_value": round(final_value, 2),
            "total_return": round(
                final_value - config.initial_cash, 2
            ),
            "total_return_pct": round(total_return, 6),
            "max_drawdown_pct": round(max_drawdown, 6),
            "sharpe_ratio_252d": round(_sharpe_ratio(equity), 4),
            "win_rate": round(win_rate, 6),
            "profit_factor": (
                round(profit_factor, 6)
                if profit_factor is not None
                else None
            ),
            "trade_count": trade_count,
            "benchmark_return_pct": round(benchmark_return, 6),
            "excess_return_pct": round(
                total_return - benchmark_return, 6
            ),
        },
        "cost_model": {
            "equivalent_fee_rate_per_side": equivalent_fee_rate,
            "slippage_rate_per_side": config.slippage_rate,
            "minimum_commission_exact": False,
            "minimum_commission": config.min_commission,
            "note": (
                "VectorBT applies an equivalent symmetric rate. "
                "Sell-only stamp tax is split across both sides; the "
                "event-driven engine remains authoritative for exact "
                "minimum commission and A-share lot handling."
            ),
        },
        "equity_curve": equity_curve,
        "trades": trade_records,
    }


def run_project_vectorbt_backtest(
    db: Session,
    *,
    symbol_ids: list[int],
    start_date: date,
    end_date: date,
    config: VectorBTConfig,
    score_weight_mode: str = "manual",
    factor_model_run_id: str | None = None,
) -> dict[str, Any]:
    frames = load_project_frames(
        db,
        symbol_ids=symbol_ids,
        start_date=start_date,
        end_date=end_date,
        score_weight_mode=score_weight_mode,
        factor_model_run_id=factor_model_run_id,
    )
    return run_vectorbt_backtest(frames, config)


__all__ = [
    "SIGNAL_MODES",
    "VectorBTConfig",
    "VectorBTInputFrames",
    "VectorBTUnavailable",
    "build_vectorbt_signals",
    "load_project_frames",
    "run_project_vectorbt_backtest",
    "run_vectorbt_backtest",
]
