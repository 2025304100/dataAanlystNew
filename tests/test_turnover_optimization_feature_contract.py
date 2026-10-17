"""Locked turnover candidates through public compiler and panel interfaces.

Every bar belongs to a temporary warehouse. These deterministic examples verify
the input contract, not market predictability, model quality or a profitable sign.
"""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.store import FactorWarehouse


CONTRACT_PATH = Path(__file__).resolve().parents[1] / (
    ".analysis/turnover-optimization-feature-candidates-2026-10-16.json"
)
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
CANDIDATES = CONTRACT["candidates"]
NOW = datetime(2026, 10, 16)
DAYS = [day.date() for day in pd.bdate_range("2026-01-05", periods=20)]


@pytest.fixture
def warehouse(tmp_path):
    store = FactorWarehouse(tmp_path / "candidate-inputs.duckdb")
    store.initialize()
    return store


def _plan(candidate):
    locked = CANDIDATES[candidate]
    result = compile_formula(
        formula=locked["formula_expr"], params=locked["params"],
        direction=locked["direction"], postprocess=locked["postprocess"],
        strict_fields=True,
    )
    assert result.is_valid, result.errors
    return result.execution_plan


def _seed(store, symbol, turnovers=None, closes=None, days=None):
    days = DAYS if days is None else days
    turnovers = [1.0] * 19 + [21.0] if turnovers is None else turnovers
    closes = [10.0] * 19 + [11.0] if closes is None else closes
    rows = [{
        "symbol": symbol, "trade_date": day, "adjust": "qfq",
        "open": 10.0, "high": 12.0, "low": 8.0, "close": close,
        "volume": 1000.0, "amount": 10000.0, "turnover_rate": turnover,
        "source": "isolated-worked-example", "source_origin": "test",
        "source_row_id": index, "source_updated_at": NOW,
        "ingested_at": NOW, "batch_id": "isolated-candidate-inputs",
    } for index, (day, turnover, close) in enumerate(zip(days, turnovers, closes))]
    store.upsert_daily_bars(rows, source_key="isolated-candidate-inputs", watermark=len(rows))


def _panel(store, candidate, day=None):
    day = DAYS[-1] if day is None else day
    result = FactorExecutor(store).execute_panel(
        _plan(candidate), start_date=day, end_date=day,
    )
    assert result.read_errors == []
    return result.factors_long.set_index("symbol").sort_index()


@pytest.mark.parametrize("candidate", ["A", "B"])
def test_locked_candidate_compiles_with_original_sandbox_limits(candidate):
    plan = _plan(candidate).to_dict()
    assert plan["execution_plan_hash"] == CANDIDATES[candidate]["execution_plan_hash"]
    assert plan["ast_depth"] <= 6
    assert plan["function_call_count"] <= 12
    assert plan["node_count"] <= 200
    assert plan["data_dependencies"]["source_tables"] == ["raw_daily_bars"]
    assert plan["max_lookback"] == 20
    assert plan["postprocess"] == {
        "winsorize": {"method": "mad", "mad_multiplier": 3.0},
        "zscore": {"ddof": 0}, "missing_policy": "exclude",
    }


@pytest.mark.parametrize("candidate,expected", [
    ("A", 2.3513752571634776), ("B", 0.23513752571634776),
])
def test_twenty_observation_worked_example_has_known_log_shock_and_price_interaction(
    warehouse, candidate, expected,
):
    # 19 observations at 1%, followed by 21%: arithmetic baseline is 2%.
    # ln(21/2) is 2.3513752571634776; 10 -> 11 multiplies it by 0.1.
    _seed(warehouse, "worked")
    assert _panel(warehouse, candidate).loc["worked", "raw_value"] == pytest.approx(expected)


@pytest.mark.parametrize("candidate", ["A", "B"])
def test_constant_nonzero_turnover_has_zero_shock_without_needing_time_series_stddev(
    warehouse, candidate,
):
    _seed(warehouse, "flat", turnovers=[2.0] * 20)
    assert _panel(warehouse, candidate).loc["flat", "raw_value"] == 0.0


@pytest.mark.parametrize("candidate", ["A", "B"])
@pytest.mark.parametrize("scale", [0.01, 100.0])
def test_uniform_turnover_unit_scale_preserves_raw_and_cross_sectional_values(
    warehouse, candidate, scale,
):
    for index, current in enumerate([1.0, 2.0, 4.0, 8.0, 16.0, 32.0]):
        _seed(warehouse, str(index), turnovers=[1.0] * 19 + [current],
              closes=[10.0] * 19 + [10.0 + index / 10.0])
    before = _panel(warehouse, candidate)
    for index, current in enumerate([1.0, 2.0, 4.0, 8.0, 16.0, 32.0]):
        _seed(warehouse, str(index), turnovers=[scale] * 19 + [current * scale],
              closes=[10.0] * 19 + [10.0 + index / 10.0])
    after = _panel(warehouse, candidate)
    np.testing.assert_allclose(after[["raw_value", "winsorized_value", "normalized_value"]].to_numpy(dtype=float),
                               before[["raw_value", "winsorized_value", "normalized_value"]].to_numpy(dtype=float),
                               rtol=1e-12, atol=1e-12)


@pytest.mark.parametrize("candidate", ["A", "B"])
def test_nineteen_bars_do_not_satisfy_twenty_observation_window(warehouse, candidate):
    _seed(warehouse, "too-new", days=DAYS[1:], turnovers=[1.0] * 18 + [21.0],
          closes=[10.0] * 18 + [11.0])
    assert pd.isna(_panel(warehouse, candidate).loc["too-new", "raw_value"])


@pytest.mark.parametrize("candidate", ["A", "B"])
@pytest.mark.parametrize("index", [9, 19], ids=["historical", "current"])
@pytest.mark.parametrize("invalid", [-1.0, None, float("nan"), float("inf"), float("-inf")],
                         ids=["negative", "missing", "nan", "pos-inf", "neg-inf"])
def test_invalid_turnover_is_excluded_instead_of_clamped_or_skipped(
    warehouse, candidate, index, invalid,
):
    turnovers = [1.0] * 19 + [21.0]
    turnovers[index] = invalid
    _seed(warehouse, "invalid", turnovers=turnovers)
    assert pd.isna(_panel(warehouse, candidate).loc["invalid", "raw_value"])


@pytest.mark.parametrize("candidate", ["A", "B"])
@pytest.mark.parametrize("flat", [False, True])
def test_current_zero_turnover_is_unavailable_even_if_history_is_finite(
    warehouse, candidate, flat,
):
    _seed(warehouse, "zero-current", turnovers=([0.0] if flat else [1.0]) * 19 + [0.0])
    assert pd.isna(_panel(warehouse, candidate).loc["zero-current", "raw_value"])


@pytest.mark.parametrize("candidate,expected", [
    ("A", 2.995732273553991), ("B", 0.2995732273553991),
])
def test_historical_zero_turnover_can_belong_to_complete_nonnegative_window(
    warehouse, candidate, expected,
):
    _seed(warehouse, "zero-history", turnovers=[0.0] * 19 + [20.0])
    assert _panel(warehouse, candidate).loc["zero-history", "raw_value"] == pytest.approx(expected)


@pytest.mark.parametrize("index", [18, 19], ids=["previous", "current"])
@pytest.mark.parametrize("invalid", [0.0, -1.0, None, float("nan"), float("inf"), float("-inf")],
                         ids=["zero", "negative", "missing", "nan", "pos-inf", "neg-inf"])
def test_price_interaction_requires_two_finite_positive_actual_closes(
    warehouse, index, invalid,
):
    closes = [10.0] * 19 + [11.0]
    closes[index] = invalid
    _seed(warehouse, "invalid-close", closes=closes)
    assert pd.isna(_panel(warehouse, "B").loc["invalid-close", "raw_value"])
    # Price availability is deliberately not an input requirement for A.
    assert _panel(warehouse, "A").loc["invalid-close", "raw_value"] == pytest.approx(2.3513752571634776)


def test_price_change_uses_previous_actual_symbol_bar_across_suspension_gap(warehouse):
    market_days = [day.date() for day in pd.bdate_range("2025-01-01", periods=120)]
    sparse_days = [market_days[index] for index in range(0, 95, 5)] + [market_days[-1]]
    _seed(warehouse, "clock", days=market_days, turnovers=[1.0] * 120, closes=[10.0] * 120)
    _seed(warehouse, "suspended", days=sparse_days)
    frame = _panel(warehouse, "B", day=market_days[-1])
    # Last actual close is 10, even though 28 other market sessions intervened.
    assert frame.loc["suspended", "raw_value"] == pytest.approx(0.23513752571634776)


def test_price_lag_does_not_cross_symbol_boundaries(warehouse):
    _seed(warehouse, "a-up")
    _seed(warehouse, "b-down", closes=[10.0] * 19 + [9.0])
    frame = _panel(warehouse, "B")
    assert frame.loc["a-up", "raw_value"] == pytest.approx(0.23513752571634776)
    assert frame.loc["b-down", "raw_value"] == pytest.approx(-0.23513752571634776)


@pytest.mark.parametrize("candidate", ["A", "B"])
def test_future_extreme_bars_cannot_change_past_panel_or_preview(warehouse, candidate):
    _seed(warehouse, "worked")
    _seed(warehouse, "flat", turnovers=[2.0] * 20)
    before = _panel(warehouse, candidate)
    preview_before = FactorExecutor(warehouse).preview(_plan(candidate), trade_date=DAYS[-1])
    future_days = [day.date() for day in pd.bdate_range("2026-02-02", periods=30)]
    _seed(warehouse, "worked", days=future_days, turnovers=[1e12] * 30, closes=[1e10] * 30)
    _seed(warehouse, "future-only", days=future_days, turnovers=[1e15] * 30, closes=[1e12] * 30)
    after = _panel(warehouse, candidate)
    preview_after = FactorExecutor(warehouse).preview(_plan(candidate), trade_date=DAYS[-1])
    pd.testing.assert_frame_equal(after, before)
    assert preview_before.errors == preview_after.errors == []
    assert preview_after.values == preview_before.values


@pytest.mark.parametrize("candidate", ["A", "B"])
def test_single_day_preview_and_panel_have_same_full_cross_section_contract(warehouse, candidate):
    for index, current in enumerate([1.0, 2.0, 4.0, 8.0, 16.0, 32.0]):
        _seed(warehouse, str(index), turnovers=[1.0] * 19 + [current],
              closes=[10.0] * 19 + [10.0 + index / 10.0])
    panel = _panel(warehouse, candidate)
    preview = FactorExecutor(warehouse).preview(_plan(candidate), trade_date=DAYS[-1], limit=100)
    assert preview.errors == []
    for row in preview.values:
        for column in ("raw_value", "winsorized_value", "normalized_value"):
            assert row[column] == pytest.approx(panel.loc[row["symbol"], column], abs=1e-12)
