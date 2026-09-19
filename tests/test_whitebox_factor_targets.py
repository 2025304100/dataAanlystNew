from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("duckdb")

from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import calculate_targets

pytestmark = pytest.mark.whitebox


def _bar(
    symbol: str,
    trade_date: date,
    row_id: int,
    *,
    open_price: float = 10,
    close: float = 10,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100,
    amount: float = 1000,
):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "adjust": "qfq",
        "universe_symbol_id": row_id,
        "open": open_price,
        "high": high if high is not None else max(open_price, close) + 0.1,
        "low": low if low is not None else min(open_price, close) - 0.1,
        "close": close,
        "volume": volume,
        "amount": amount,
        "turnover_rate": 1,
        "source": "test",
        "source_origin": "universe_daily_bars",
        "source_row_id": row_id,
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "target-source",
    }


def test_target_uses_t1_open_t5_close_and_is_idempotent(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(7)]
    bars = []
    for index, trade_date in enumerate(dates):
        bars.append(
            _bar(
                "000001",
                trade_date,
                index + 1,
                open_price=10 + index,
                close=10 + index,
            )
        )
    warehouse.upsert_daily_bars(
        bars, source_key="target.bars", watermark=len(bars)
    )

    first = calculate_targets(
        warehouse,
        start_date=dates[0],
        end_date=dates[0],
        calc_batch_id="target-test",
    )
    second = calculate_targets(
        warehouse,
        start_date=dates[0],
        end_date=dates[0],
        calc_batch_id="target-test",
    )

    assert first.rows_written == 1
    assert first.tradable_rows == 1
    assert second.rows_written == 1
    with warehouse.connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT entry_date, exit_date, target_value, is_tradable "
            "FROM factor_targets WHERE calc_batch_id = 'target-test'"
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM factor_targets "
            "WHERE calc_batch_id = 'target-test'"
        ).fetchone()[0]
    assert count == 1
    assert row[0] == dates[1]
    assert row[1] == dates[5]
    assert row[2] == pytest.approx(15 / 11 - 1)
    assert row[3] is True


def test_target_uses_market_calendar_to_detect_suspension(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(7)]
    bars = []
    row_id = 0
    for trade_date in dates:
        row_id += 1
        bars.append(_bar("000001", trade_date, row_id))
    for index, trade_date in enumerate(dates):
        if index == 1:
            continue
        row_id += 1
        bars.append(_bar("000002", trade_date, row_id))
    warehouse.upsert_daily_bars(
        bars, source_key="target.bars", watermark=row_id
    )

    calculate_targets(
        warehouse,
        start_date=dates[0],
        end_date=dates[0],
        calc_batch_id="suspension-test",
    )

    with warehouse.connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT target_value, is_tradable, invalid_reason "
            "FROM factor_targets WHERE symbol = '000002' "
            "AND calc_batch_id = 'suspension-test'"
        ).fetchone()
    assert row == (None, False, "missing_entry_bar")


def test_target_rejects_locked_limit_up_and_incomplete_future(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(7)]
    bars = []
    for index, trade_date in enumerate(dates):
        if index == 0:
            bars.append(_bar("000001", trade_date, index + 1, close=10))
        elif index == 1:
            bars.append(
                _bar(
                    "000001",
                    trade_date,
                    index + 1,
                    open_price=11,
                    close=11,
                    high=11,
                    low=11,
                )
            )
        else:
            bars.append(_bar("000001", trade_date, index + 1))
    warehouse.upsert_daily_bars(
        bars, source_key="target.bars", watermark=len(bars)
    )

    calculate_targets(warehouse, calc_batch_id="invalid-test")

    with warehouse.connection(read_only=True) as conn:
        first = conn.execute(
            "SELECT invalid_reason FROM factor_targets "
            "WHERE signal_date = ? AND calc_batch_id = 'invalid-test'",
            [dates[0]],
        ).fetchone()
        last = conn.execute(
            "SELECT target_value, is_tradable, invalid_reason "
            "FROM factor_targets WHERE signal_date = ? "
            "AND calc_batch_id = 'invalid-test'",
            [dates[-1]],
        ).fetchone()
    assert first[0] == "entry_locked_limit_up"
    assert last == (None, False, "insufficient_future_calendar")


def test_target_validates_arguments(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    with pytest.raises(ValueError, match="start_date"):
        calculate_targets(
            warehouse,
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 1),
        )
    with pytest.raises(ValueError, match="limit_threshold"):
        calculate_targets(warehouse, limit_threshold=0)
