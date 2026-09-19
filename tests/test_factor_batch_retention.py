from datetime import date, datetime, timedelta

from app.services.factors.store import FactorWarehouse


def _factor_row(batch_id: str, created_at: datetime):
    return {
        "symbol": "600000",
        "trade_date": date(2026, 8, 1),
        "factor_code": "value_pe",
        "factor_version": 1,
        "raw_value": 1.0,
        "winsorized_value": 1.0,
        "normalized_value": 0.0,
        "is_imputed": False,
        "imputation_method": None,
        "eligible": True,
        "data_cutoff_at": created_at,
        "calc_batch_id": batch_id,
        "created_at": created_at,
    }


def _target_row(batch_id: str, created_at: datetime):
    return {
        "symbol": "600000",
        "signal_date": date(2026, 8, 1),
        "entry_date": date(2026, 8, 2),
        "exit_date": date(2026, 8, 7),
        "target_code": "target_5d_return",
        "target_value": 0.1,
        "is_tradable": True,
        "invalid_reason": None,
        "calc_batch_id": batch_id,
        "created_at": created_at,
    }


def test_prune_keeps_recent_batches_and_explicit_protected_batch(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "retention.duckdb")
    base = datetime(2026, 8, 1, 10, 0, 0)
    for index in range(4):
        created = base + timedelta(days=index)
        batch = f"batch-{index}"
        warehouse.upsert_records("factor_values", [_factor_row(batch, created)])
        warehouse.upsert_records("factor_targets", [_target_row(batch, created)])

    result = warehouse.prune_calculation_batches(
        keep_batches=2,
        protected_batch_ids=["batch-0"],
    )

    assert result["deleted_batch_ids"] == ["batch-1"]
    assert result["factor_value_rows_deleted"] == 1
    assert result["factor_target_rows_deleted"] == 1
    with warehouse.connection(read_only=True) as conn:
        remaining = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT calc_batch_id FROM factor_values"
            ).fetchall()
        }
    assert remaining == {"batch-0", "batch-2", "batch-3"}


def test_prune_does_not_delete_when_within_retention(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "retention-small.duckdb")
    warehouse.upsert_records(
        "factor_values",
        [_factor_row("only", datetime(2026, 8, 1, 10, 0, 0))],
    )
    result = warehouse.prune_calculation_batches(keep_batches=2)
    assert result["deleted_batch_ids"] == []
    assert result["factor_value_rows_deleted"] == 0

