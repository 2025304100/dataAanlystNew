from datetime import date, datetime, timedelta

import pytest

from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import calculate_targets
from app.services.factors.label_batch import resolve_label_batch
from app.services.factors.mining.contracts import MiningContext
from app.services.factors.mining.evaluation_adapter import build_full_panels
from app.services.factors.factor_evaluator import TimeSplit

pytestmark = pytest.mark.whitebox


@pytest.fixture
def scoped_warehouse(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "scoped-mining.duckdb")
    start = date(2026, 1, 5)
    bars = []
    for index in range(40):
        for symbol, close in (("000001", 10.0), ("000002", 20.0), ("999999", 100.0)):
            bars.append({"symbol": symbol, "trade_date": start + timedelta(days=index),
                "adjust": "qfq", "open": close, "high": close + 1, "low": close - 1,
                "close": close, "volume": 100, "amount": 1000, "turnover_rate": 1,
                "source": "isolated-test", "source_origin": "daily_bars",
                "source_row_id": len(bars) + 1, "ingested_at": datetime(2026, 1, 5),
                "batch_id": "scope-test"})
    warehouse.upsert_daily_bars(bars, source_key="scope-test", watermark=len(bars))
    return warehouse, start


def test_cross_section_is_computed_inside_the_frozen_candidate_scope(scoped_warehouse):
    warehouse, start = scoped_warehouse
    plan = compile_formula(formula="cs_rank(close)", params=None).execution_plan
    outcome = FactorExecutor(warehouse).execute_panel(plan, start_date=start,
        end_date=start, symbols=["000001", "000002"])
    assert outcome.read_errors == []
    assert outcome.symbols == ["000001", "000002"]
    assert dict(zip(outcome.factors_long["symbol"], outcome.factors_long["raw_value"])) == {
        "000001": 0.5, "000002": 1.0,
    }


def test_one_day_target_uses_the_next_trading_day_close(scoped_warehouse):
    warehouse, start = scoped_warehouse
    result = calculate_targets(warehouse, start_date=start, end_date=start,
        calc_batch_id="one-day-target", target_horizon=1)
    panel, _, code = warehouse.get_target_panel(result.calc_batch_id, "target_1d_return",
        as_of_exit_date=start + timedelta(days=1))
    assert code == "target_1d_return"
    assert len(panel) == 3
    earlier, _, _ = warehouse.get_target_panel(result.calc_batch_id, "target_1d_return", as_of_exit_date=start)
    assert earlier.empty
    assert dict(zip(panel["symbol"], panel["target_value"])) == {
        "000001": 0.0, "000002": 0.0, "999999": 0.0,
    }


def test_mining_can_resolve_one_day_labels_at_the_frozen_cutoff(scoped_warehouse):
    warehouse, start = scoped_warehouse
    first = resolve_label_batch(warehouse, start_date=start,
        end_date=start + timedelta(days=39), target_code="target_1d_return",
        as_of_exit_date=start + timedelta(days=39), allow_horizon_backfill=True)
    assert first.coverage.satisfied is True
    assert first.usable_end == start + timedelta(days=38)
    assert first.coverage.covered_days == 39
    assert first.target_code == "target_1d_return"
    again = resolve_label_batch(warehouse, start_date=start,
        end_date=start + timedelta(days=39), target_code="target_1d_return",
        as_of_exit_date=start + timedelta(days=39), allow_horizon_backfill=True)
    assert again.batch_id == first.batch_id
    assert again.rows_written == 0 and not again.backfilled


def test_twenty_day_target_uses_actual_mature_exit_not_the_default_five_days(scoped_warehouse):
    warehouse, start = scoped_warehouse
    # Vary prices so this proves the return horizon as well as PIT exit dates.
    with warehouse.connection() as conn:
        conn.execute("UPDATE raw_daily_bars SET open = 10 + date_diff('day', ?, trade_date), "
            "close = 10 + date_diff('day', ?, trade_date), "
            "high = 11 + date_diff('day', ?, trade_date), "
            "low = 9 + date_diff('day', ?, trade_date) WHERE symbol = '000001'",
            [start, start, start, start])
    calculate_targets(warehouse, start_date=start, end_date=start,
        calc_batch_id="twenty-day-target", target_horizon=20)
    immature, _, _ = warehouse.get_target_panel("twenty-day-target", "target_20d_return",
        as_of_exit_date=start + timedelta(days=19))
    assert immature.empty
    mature, _, _ = warehouse.get_target_panel("twenty-day-target", "target_20d_return",
        as_of_exit_date=start + timedelta(days=20))
    actual = mature[mature["symbol"] == "000001"]["target_value"].iloc[0]
    assert actual == pytest.approx(30 / 11 - 1)


def test_final_panels_use_the_same_frozen_symbols_and_signal_dates(scoped_warehouse):
    warehouse, start = scoped_warehouse
    calculate_targets(warehouse, calc_batch_id="full-panel-targets")
    signals = tuple(start + timedelta(days=offset) for offset in (4, 11, 18, 25, 32))
    ctx = MiningContext(run_id="scope-run", candidate_pool_snapshot_id="frozen-scope",
        data_cutoff_at=datetime(2026, 2, 13), start_date=start,
        end_date=start + timedelta(days=39), rebalance_frequency="weekly", target_horizon=5,
        split=TimeSplit(train_start=start, train_end=start + timedelta(days=10),
            validation_start=start + timedelta(days=11), validation_end=start + timedelta(days=20),
            test_start=start + timedelta(days=21), test_end=start + timedelta(days=34)),
        purge_points=1, embargo_points=1, train_ratio=0.6, validation_ratio=0.2,
        random_seed=0, config_hash="scope-contract", target_calc_batch_id="full-panel-targets",
        warehouse_path=str(warehouse.path), candidate_symbols=("000001", "000002"), signal_dates=signals)
    values, returns = build_full_panels(ctx, "cs_rank(close)")
    assert tuple(values.index) == tuple(returns.index) == signals
    assert tuple(values.columns) == tuple(returns.columns) == ("000001", "000002")
    assert values["000001"].tolist() == [0.5] * 5
    assert values["000002"].tolist() == [1.0] * 5
