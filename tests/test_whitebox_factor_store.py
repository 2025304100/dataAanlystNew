from __future__ import annotations

from datetime import date, datetime

import pytest

pytest.importorskip("duckdb")

from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def _bar(**overrides):
    row = {
        "symbol": "600519",
        "trade_date": date(2026, 7, 10),
        "adjust": "qfq",
        "business_symbol_id": 10,
        "universe_symbol_id": None,
        "open": 1400.0,
        "high": 1420.0,
        "low": 1390.0,
        "close": 1410.0,
        "volume": 100.0,
        "amount": 141000.0,
        "turnover_rate": 0.5,
        "source": "test",
        "source_origin": "daily_bars",
        "source_row_id": 1,
        "source_updated_at": datetime(2026, 7, 10, 10, 0),
        "ingested_at": datetime(2026, 7, 13, 10, 0),
        "batch_id": "batch-1",
    }
    row.update(overrides)
    return row


def test_initialize_and_health(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    assert warehouse.health().available is False

    warehouse.initialize()
    health = warehouse.health()

    assert health.available is True
    assert health.schema_version == "3"
    assert health.raw_daily_bars == 0


def test_health_remains_readable_during_same_process_write_connection(
    tmp_path,
):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    warehouse.initialize()

    with warehouse.connection() as writer:
        writer.execute("BEGIN TRANSACTION")
        try:
            health = warehouse.health()
            assert health.available is True
            assert health.schema_version == "3"
        finally:
            writer.execute("ROLLBACK")


def test_daily_bar_upsert_is_idempotent_and_preserves_source_ids(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")

    assert warehouse.upsert_daily_bars(
        [_bar()], source_key="business", watermark=1
    ) == 1
    assert warehouse.upsert_daily_bars(
        [
            _bar(
                business_symbol_id=None,
                universe_symbol_id=20,
                close=1415.0,
                source_origin="universe_daily_bars",
                source_row_id=7,
                batch_id="batch-2",
            )
        ],
        source_key="universe",
        watermark=7,
    ) == 1

    with warehouse.connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*), MAX(close), MAX(business_symbol_id), "
            "MAX(universe_symbol_id) FROM raw_daily_bars"
        ).fetchone()

    assert row == (1, 1415.0, 10, 20)
    assert warehouse.get_watermark("business") == 1
    assert warehouse.get_watermark("universe") == 7


def test_failed_batch_does_not_advance_watermark(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    warehouse.initialize()

    with pytest.raises(Exception):
        warehouse.upsert_daily_bars(
            [_bar(symbol=None)],
            source_key="business",
            watermark=99,
        )

    assert warehouse.get_watermark("business") == 0
    assert warehouse.health().raw_daily_bars == 0


def test_store_bulk_frame_upsert_is_idempotent(tmp_path):
    import pandas as pd

    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    frame = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": date(2026, 7, 10),
                "factor_code": "test",
                "factor_version": 1,
                "raw_value": 1.0,
                "winsorized_value": 1.0,
                "normalized_value": 0.0,
                "is_imputed": False,
                "imputation_method": None,
                "eligible": True,
                "data_cutoff_at": datetime(2026, 7, 10),
                "calc_batch_id": "bulk-test",
                "created_at": datetime(2026, 7, 10),
            }
        ]
    )

    assert warehouse.upsert_frame("factor_values", frame) == 1
    assert warehouse.upsert_frame("factor_values", frame) == 1
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM factor_values"
        ).fetchone()[0] == 1
