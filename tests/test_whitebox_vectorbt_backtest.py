from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.vectorbt_backtest import (
    VectorBTConfig,
    VectorBTInputFrames,
    build_vectorbt_signals,
    load_project_frames,
    run_vectorbt_backtest,
)
from scripts.backtest_vectorbt import _parser


pytestmark = pytest.mark.whitebox


def _frames(close_values, *, with_scores: bool = True):
    index = pd.date_range("2026-01-01", periods=len(close_values), freq="D")
    close = pd.DataFrame({"000001": close_values}, index=index, dtype=float)
    quality_value = 70.0 if with_scores else np.nan
    timing_value = 65.0 if with_scores else np.nan
    quality = pd.DataFrame(
        quality_value, index=index, columns=close.columns
    )
    timing = pd.DataFrame(
        timing_value, index=index, columns=close.columns
    )
    ranking = pd.DataFrame(
        80.0 if with_scores else np.nan,
        index=index,
        columns=close.columns,
    )
    return VectorBTInputFrames(
        close=close,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        quality=quality,
        timing=timing,
        ranking=ranking,
        symbol_ids={"000001": 1},
        score_weight_mode="manual",
        factor_model_run_id=None,
    )


def test_vectorbt_config_rejects_invalid_windows():
    with pytest.raises(ValueError, match="windows"):
        VectorBTConfig(fast_window=20, slow_window=10).validate()


def test_vectorbt_cli_parser_exposes_top_n_option():
    parser = _parser()
    action = next(
        item for item in parser._actions if item.dest == "max_positions"
    )
    assert action.default == 10
    assert "Top-N" in str(action.help)


def test_score_trend_requires_score_coverage():
    config = VectorBTConfig(
        signal_mode="score_trend",
        fast_window=2,
        slow_window=3,
        execution_lag=0,
    )
    with pytest.raises(ValueError, match="Score coverage"):
        build_vectorbt_signals(
            _frames([10, 10, 11, 12, 11], with_scores=False),
            config,
        )


def test_ma_cross_signals_are_shifted_to_avoid_same_close_execution():
    frames = _frames([10, 10, 11, 12, 13, 12, 11, 10])
    same_close = VectorBTConfig(
        signal_mode="ma_cross",
        fast_window=2,
        slow_window=3,
        execution_lag=0,
    )
    next_row = VectorBTConfig(
        signal_mode="ma_cross",
        fast_window=2,
        slow_window=3,
        execution_lag=1,
    )

    entries_now, exits_now = build_vectorbt_signals(frames, same_close)
    entries_next, exits_next = build_vectorbt_signals(frames, next_row)

    assert entries_now["000001"].sum() == 1
    assert exits_now["000001"].sum() == 1
    assert entries_next["000001"].idxmax() > entries_now["000001"].idxmax()
    assert exits_next["000001"].idxmax() > exits_now["000001"].idxmax()


def test_score_trend_selects_daily_top_n_by_ranking_score():
    index = pd.date_range("2026-01-01", periods=6, freq="D")
    columns = ["600000", "000001", "300001"]
    close = pd.DataFrame(
        {column: [10, 11, 12, 13, 14, 15] for column in columns},
        index=index,
        dtype=float,
    )
    frames = VectorBTInputFrames(
        close=close,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        quality=pd.DataFrame(70.0, index=index, columns=columns),
        timing=pd.DataFrame(65.0, index=index, columns=columns),
        ranking=pd.DataFrame(
            {
                "600000": [80, 80, 90, 95, 95, 80],
                "000001": [80, 80, 90, 80, 80, 96],
                "300001": [70, 70, 70, 70, 70, 70],
            },
            index=index,
        ),
        symbol_ids={column: offset for offset, column in enumerate(columns, 1)},
        score_weight_mode="manual",
        factor_model_run_id=None,
    )

    entries, _ = build_vectorbt_signals(
        frames,
        VectorBTConfig(
            signal_mode="score_trend",
            fast_window=2,
            slow_window=3,
            max_positions=1,
            execution_lag=0,
        ),
    )

    assert entries.sum(axis=1).max() == 1
    assert bool(entries.loc[index[2], "000001"])
    assert bool(entries.loc[index[3], "600000"])
    assert bool(entries.loc[index[5], "000001"])

def test_vectorbt_engine_returns_portfolio_metrics_and_trades():
    result = run_vectorbt_backtest(
        _frames([10, 10, 11, 12, 13, 12, 11, 10]),
        VectorBTConfig(
            signal_mode="ma_cross",
            initial_cash=100_000,
            fast_window=2,
            slow_window=3,
            execution_lag=0,
            position_pct=0.5,
            max_positions=2,
            disable_numba=True,
        ),
    )

    assert result["engine"]["name"] == "vectorbt"
    assert result["engine"]["version"] == "0.28.5"
    assert result["metrics"]["trade_count"] == 1
    assert result["signals"] == {"entries": 1, "exits": 1}
    assert len(result["equity_curve"]) == 8
    assert result["cost_model"]["minimum_commission_exact"] is False
    assert result["selection"] == {
        "mode": "none",
        "ranking_field": "priority_score",
        "top_n": 2,
    }
    assert result["trades"]


def test_load_project_frames_aligns_prices_and_latest_scores(db_session):
    symbol = Symbol(
        symbol="000001",
        name="平安银行",
        asset_type="stock",
        market="sz",
    )
    db_session.add(symbol)
    db_session.flush()
    start = date(2026, 1, 1)
    for offset in range(5):
        trade_date = start + timedelta(days=offset)
        price = 10.0 + offset
        db_session.add(
            DailyBar(
                symbol_id=symbol.id,
                trade_date=trade_date,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=100,
                amount=1000,
                source="test",
            )
        )
    db_session.add_all(
        [
            Score(
                symbol_id=symbol.id,
                trade_date=start,
                quality_score=60,
                quality_grade="B",
                timing_score=55,
                stage="start",
                action="watch",
                priority_score=58,
                weight_mode="manual",
                calc_batch_id="score-1",
            ),
            Score(
                symbol_id=symbol.id,
                trade_date=start + timedelta(days=2),
                quality_score=70,
                quality_grade="A",
                timing_score=65,
                stage="accel",
                action="buy",
                priority_score=68,
                weight_mode="manual",
                calc_batch_id="score-2",
            ),
            Score(
                symbol_id=symbol.id,
                trade_date=start,
                quality_score=75,
                quality_grade="A",
                timing_score=66,
                stage="accel",
                action="buy",
                priority_score=99,
                weight_mode="ridge",
                factor_model_run_id="ridge-1",
                factor_quality_score=75,
                factor_timing_score=66,
                model_alpha_score=0.42,
                calc_batch_id="ridge-score-1",
            ),
        ]
    )
    db_session.commit()

    frames = load_project_frames(
        db_session,
        symbol_ids=[symbol.id],
        start_date=start,
        end_date=start + timedelta(days=4),
    )

    assert frames.close.shape == (5, 1)
    assert frames.quality.iloc[1, 0] == 60
    assert frames.quality.iloc[4, 0] == 70
    assert frames.timing.iloc[4, 0] == 65
    assert frames.ranking.iloc[1, 0] == 58
    assert frames.ranking.iloc[4, 0] == 68

    ridge_frames = load_project_frames(
        db_session,
        symbol_ids=[symbol.id],
        start_date=start,
        end_date=start + timedelta(days=4),
        score_weight_mode="ridge",
        factor_model_run_id="ridge-1",
    )
    assert ridge_frames.quality.iloc[0, 0] == 75
    assert ridge_frames.timing.iloc[0, 0] == 66
    assert ridge_frames.ranking.iloc[0, 0] == 0.42
