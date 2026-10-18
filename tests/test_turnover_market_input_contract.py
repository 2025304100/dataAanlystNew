"""Public source normalization, factor panels and target previews on memory DuckDB.

No project database, market provider, training or runtime activation is used.
"""
from contextlib import contextmanager
from datetime import date

import duckdb
import pandas as pd
import pytest

from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.target_engine import preview_targets
from app.services.market_data_sources.sources._normalize import (
    normalize_cn_em_history,
    normalize_cn_stock_sina_history,
)

pytestmark = pytest.mark.whitebox


TURNOVER = (
    "iif(count(turnover_rate - turnover_rate, 20) == 20, "
    "iif(stddev(turnover_rate, 20) > 1e-8, "
    "(turnover_rate - mean(turnover_rate, 20)) / stddev(turnover_rate, 20), 0), None)"
)


class MemoryWarehouse:
    def __init__(self):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute("""CREATE TABLE raw_daily_bars (
            symbol VARCHAR, trade_date DATE, adjust VARCHAR, open DOUBLE,
            high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, amount DOUBLE,
            turnover_rate DOUBLE)""")

    @contextmanager
    def connection(self, *, read_only=True):
        assert read_only
        yield self.conn

    def list_trade_dates(self):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT trade_date FROM raw_daily_bars ORDER BY trade_date DESC"
        ).fetchall()]

    def seed(self, rows):
        self.conn.executemany(
            "INSERT INTO raw_daily_bars VALUES (?,?, 'qfq',?,?,?,?,?,?,?)", rows
        )


@pytest.fixture
def warehouse():
    warehouse = MemoryWarehouse()
    yield warehouse
    warehouse.conn.close()


def _plan():
    result = compile_formula(formula=TURNOVER, direction="nonlinear",
        postprocess={"missing_policy": "exclude"})
    assert result.success, result.errors
    return result.execution_plan


def test_same_two_percent_turnover_from_em_and_sina_never_creates_source_switch_signal(warehouse):
    """EM 2.0 percent and Sina 0.02 ratio are one economic quantity."""
    days = [d.date() for d in pd.bdate_range("2026-01-01", periods=20)]
    em = normalize_cn_em_history(pd.DataFrame([{
        "日期": day, "开盘": 10, "最高": 11, "最低": 9, "收盘": 10,
        "成交量": 100, "成交额": 1000, "换手率": 2.0,
    } for day in days[:19]]))
    sina = normalize_cn_stock_sina_history(pd.DataFrame([{
        "date": days[-1], "open": 10, "high": 11, "low": 9, "close": 10,
        "volume": 100, "amount": 1000, "turnover": .02,
    }]), days[-1], days[-1])
    assert em.turnover_rate.tolist() == [2.0] * 19
    assert sina.turnover_rate.tolist() == [2.0]
    mixed = pd.concat([em, sina], ignore_index=True)
    warehouse.seed([("mixed", r.trade_date.date(), r.open, r.high, r.low,
        r.close, r.volume, r.amount, r.turnover_rate) for r in mixed.itertuples()])
    panel = FactorExecutor(warehouse).execute_panel(
        _plan(), start_date=days[-1], end_date=days[-1])
    assert panel.read_errors == []
    assert panel.n_cells == 1
    assert panel.factors_long.iloc[0].raw_value == 0.0


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("bar_index,field,reason", [
    (1, "open", "entry_non_finite_values"),
    (1, "high", "entry_non_finite_values"),
    (1, "low", "entry_non_finite_values"),
    (1, "volume", "entry_non_finite_values"),
    (1, "amount", "entry_non_finite_values"),
    (0, "close", "entry_non_finite_values"),
    (5, "close", "exit_non_finite_values"),
    (5, "high", "exit_non_finite_values"),
    (5, "low", "exit_non_finite_values"),
    (5, "volume", "exit_non_finite_values"),
    (5, "amount", "exit_non_finite_values"),
    (4, "close", "exit_non_finite_values"),
])
def test_target_preview_excludes_non_finite_prices_and_liquidity(
    warehouse, bar_index, field, reason, bad,
):
    days = [d.date() for d in pd.bdate_range("2026-07-01", periods=7)]
    rows = []
    for index, day in enumerate(days):
        bar = dict(open=10., high=11., low=9., close=10., volume=100., amount=1000.)
        if index == bar_index:
            bar[field] = bad
        rows.append(("invalid", day, *bar.values(), 1.))
    warehouse.seed(rows)
    result = preview_targets(warehouse, start_date=days[0], end_date=days[0])
    row = result.iloc[0]
    assert row.entry_date == days[1] and row.exit_date == days[5]
    assert not row.is_tradable
    assert pd.isna(row.target_value)
    assert row.invalid_reason == reason


def test_target_preview_still_uses_market_t_plus_one_when_stock_is_suspended(warehouse):
    days = [d.date() for d in pd.bdate_range("2026-07-01", periods=7)]
    warehouse.seed([("clock", d, 10, 11, 9, 10, 100, 1000, 1.) for d in days])
    warehouse.seed([("suspended", d, 10, 11, 9, 10, 100, 1000, 1.)
                    for i, d in enumerate(days) if i != 1])
    result = preview_targets(warehouse, start_date=days[0], end_date=days[0])
    row = result[result.symbol == "suspended"].iloc[0]
    assert row.entry_date == days[1]
    assert not row.is_tradable and pd.isna(row.target_value)
    assert row.invalid_reason == "missing_entry_bar"


@pytest.mark.parametrize("interface", ["panel", "preview"])
@pytest.mark.parametrize("future", [False, True])
def test_sparse_stock_uses_twenty_real_observations_across_120_market_dates(
    warehouse, interface, future,
):
    days = [d.date() for d in pd.bdate_range("2025-01-01", periods=120)]
    indices = sorted(set(range(0, 120, 5)) | {119})
    warehouse.seed([("clock", d, 10, 11, 9, 10, 100, 1000, 1.) for d in days])
    warehouse.seed([("sparse", days[i], 10, 11, 9, 10, 100, 1000, float(j + 1))
        for j, i in enumerate(indices)])
    if future:
        future_days = [d.date() for d in pd.bdate_range(days[-1] + pd.Timedelta(days=1), periods=30)]
        warehouse.seed([("sparse", d, 10, 11, 9, 10, 100, 1000, 1_000_000.) for d in future_days])
    executor = FactorExecutor(warehouse)
    if interface == "panel":
        result = executor.execute_panel(_plan(), start_date=days[-1], end_date=days[-1], symbols=["sparse"])
        assert result.read_errors == []
        assert result.n_cells == 1
        row = result.factors_long.iloc[0]
        assert row.trade_date == days[-1] and row.symbol == "sparse"
        value = row.raw_value
    else:
        result = executor.preview(_plan(), trade_date=days[-1], symbols=["sparse"])
        assert result.errors == []
        assert len(result.values) == 1
        assert pd.Timestamp(result.values[0]["trade_date"]).date() == days[-1]
        value = result.values[0]["raw_value"]
    # Hand-checked population Z for the final observation of 6,7,...,25.
    assert value == pytest.approx(1.647508942095828, abs=1e-12)


@pytest.mark.parametrize("incomplete", [True, False])
def test_sparse_warmup_never_skips_missing_values_or_invents_observations(warehouse, incomplete):
    days = [d.date() for d in pd.bdate_range("2025-01-01", periods=120)]
    indices = sorted(set(range(0, 120, 5)) | {119})
    if incomplete:
        indices = indices[-19:]
    warehouse.seed([("clock", d, 10, 11, 9, 10, 100, 1000, 1.) for d in days])
    warehouse.seed([("sparse", days[i], 10, 11, 9, 10, 100, 1000,
        None if not incomplete and j == len(indices) - 10 else float(j + 1))
        for j, i in enumerate(indices)])
    result = FactorExecutor(warehouse).execute_panel(
        _plan(), start_date=days[-1], end_date=days[-1], symbols=["sparse"])
    assert result.read_errors == [] and result.n_cells == 1
    assert pd.isna(result.factors_long.iloc[0].raw_value)


def test_sparse_warmup_is_complete_at_window_start_even_when_later_rows_are_dense(warehouse):
    days = [d.date() for d in pd.bdate_range("2025-01-01", periods=200)]
    sparse_indices = sorted(set(range(0, 120, 5)) | {119})
    indices = sparse_indices + list(range(120, 200))
    warehouse.seed([("clock", d, 10, 11, 9, 10, 100, 1000, 1.) for d in days])
    warehouse.seed([("sparse", days[i], 10, 11, 9, 10, 100, 1000, float(j + 1))
        for j, i in enumerate(indices)])
    result = FactorExecutor(warehouse).execute_panel(
        _plan(), start_date=days[119], end_date=days[-1], symbols=["sparse"])
    assert result.read_errors == []
    assert result.n_cells == 81
    assert result.trade_dates[0] == days[119] and result.trade_dates[-1] == days[-1]
    assert result.factors_long.iloc[0].raw_value == pytest.approx(1.647508942095828, abs=1e-12)
