"""WPD-06: White-box tests for the batch audit module."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

pytest.importorskip("duckdb")

from app.services.factors.batch_audit import (
    BatchAuditReport,
    BatchLagDiagnostic,
    BatchRecord,
    audit_batch_atomicity,
    batch_context,
    begin_batch,
    diagnose_factor_lag,
    finalize_batch,
    get_batch_audit_report,
    list_recent_batches,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def _make_warehouse(tmp_path) -> FactorWarehouse:
    warehouse = FactorWarehouse(tmp_path / "batch_audit.duckdb")
    warehouse.initialize()
    return warehouse


def _insert_raw_daily_bar(
    warehouse: FactorWarehouse,
    *,
    batch_id: str,
    trade_date: date,
    symbol: str = "600519",
) -> None:
    with warehouse._write_lock, warehouse.connection() as conn:
        conn.execute(
            "INSERT INTO raw_daily_bars "
            "(symbol, trade_date, adjust, business_symbol_id, "
            " universe_symbol_id, open, high, low, close, volume, amount, "
            " turnover_rate, source, source_origin, source_row_id, "
            " source_updated_at, ingested_at, batch_id) "
            "VALUES (?, ?, 'qfq', NULL, NULL, 10, 11, 9, 10, 100, 1000, "
            " 0.5, 'test', 'daily_bars', 1, NULL, ?, ?)"
            " ON CONFLICT (symbol, trade_date, adjust) DO UPDATE SET "
            "  batch_id = excluded.batch_id",
            [symbol, trade_date, datetime(2026, 7, 1), batch_id],
        )


def _insert_factor_value(
    warehouse: FactorWarehouse,
    *,
    calc_batch_id: str,
    trade_date: date,
    symbol: str = "600519",
    factor_code: str = "turnover_z20",
) -> None:
    with warehouse._write_lock, warehouse.connection() as conn:
        conn.execute(
            "INSERT INTO factor_values "
            "(symbol, trade_date, factor_code, factor_version, raw_value, "
            " winsorized_value, normalized_value, is_imputed, "
            " imputation_method, eligible, data_cutoff_at, calc_batch_id, "
            " created_at) "
            "VALUES (?, ?, ?, 1, 1.0, 1.0, 0.5, FALSE, NULL, TRUE, ?, ?, ?)",
            [
                symbol,
                trade_date,
                factor_code,
                datetime(2026, 7, 1),
                calc_batch_id,
                datetime(2026, 7, 1),
            ],
        )


# ---------------------------------------------------------------------------
# 1. begin_batch creates running record
# ---------------------------------------------------------------------------


def test_begin_batch_creates_running_record(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    begin_batch(
        warehouse,
        batch_id="test-batch-1",
        source_key="ingest.daily_bars.business",
        scope={"start_date": "2026-07-01"},
    )
    records = list_recent_batches(warehouse, limit=10)
    assert len(records) == 1
    assert records[0].batch_id == "test-batch-1"
    assert records[0].status == "running"
    assert records[0].finished_at is None
    assert records[0].rows_written == 0


# ---------------------------------------------------------------------------
# 2. finalize_batch committed
# ---------------------------------------------------------------------------


def test_finalize_batch_committed(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    begin_batch(
        warehouse,
        batch_id="test-batch-2",
        source_key="ingest.daily_bars.business",
    )
    finalize_batch(
        warehouse,
        batch_id="test-batch-2",
        status="committed",
        rows_received=100,
        rows_written=100,
    )
    records = list_recent_batches(warehouse, limit=10)
    assert records[0].status == "committed"
    assert records[0].finished_at is not None
    assert records[0].rows_written == 100
    assert records[0].rows_received == 100


# ---------------------------------------------------------------------------
# 3. finalize_batch failed
# ---------------------------------------------------------------------------


def test_finalize_batch_failed_with_error(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    begin_batch(
        warehouse,
        batch_id="test-batch-3",
        source_key="ingest.daily_bars.business",
    )
    finalize_batch(
        warehouse,
        batch_id="test-batch-3",
        status="failed",
        error={"error_type": "ValueError", "error_message": "bad data"},
    )
    records = list_recent_batches(warehouse, limit=10)
    assert records[0].status == "failed"
    assert records[0].error_json is not None
    error = json.loads(records[0].error_json)
    assert error["error_type"] == "ValueError"
    assert "bad data" in error["error_message"]


# ---------------------------------------------------------------------------
# 4. batch_context success
# ---------------------------------------------------------------------------


def test_batch_context_success_marks_committed(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    with batch_context(
        warehouse,
        batch_id="ctx-ok",
        source_key="factor.calculation",
    ):
        pass
    records = list_recent_batches(warehouse, limit=10)
    assert len(records) == 1
    assert records[0].status == "committed"
    assert records[0].finished_at is not None


# ---------------------------------------------------------------------------
# 5. batch_context exception marks failed
# ---------------------------------------------------------------------------


def test_batch_context_exception_marks_failed(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        with batch_context(
            warehouse,
            batch_id="ctx-fail",
            source_key="factor.calculation",
        ):
            raise RuntimeError("injected failure")
    records = list_recent_batches(warehouse, limit=10)
    assert len(records) == 1
    assert records[0].status == "failed"
    error = json.loads(records[0].error_json)
    assert error["error_type"] == "RuntimeError"
    assert "injected failure" in error["error_message"]


# ---------------------------------------------------------------------------
# 6. list_recent_batches returns N sorted by started_at DESC
# ---------------------------------------------------------------------------


def test_list_recent_batches_orders_by_started_at_desc(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    for index in range(5):
        begin_batch(
            warehouse,
            batch_id=f"order-{index}",
            source_key="ingest.daily_bars.business",
        )
        time.sleep(0.01)
    records = list_recent_batches(warehouse, limit=10)
    assert len(records) == 5
    started = [r.started_at for r in records]
    assert started == sorted(started, reverse=True)


# ---------------------------------------------------------------------------
# 7. list_recent_batches filter by source_key
# ---------------------------------------------------------------------------


def test_list_recent_batches_filters_by_source_key(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    begin_batch(
        warehouse,
        batch_id="filter-a",
        source_key="ingest.daily_bars.business",
    )
    begin_batch(
        warehouse,
        batch_id="filter-b",
        source_key="factor.calculation",
    )
    begin_batch(
        warehouse,
        batch_id="filter-c",
        source_key="ingest.daily_bars.business",
    )
    bars = list_recent_batches(
        warehouse, source_key="ingest.daily_bars.business", limit=10
    )
    factors = list_recent_batches(
        warehouse, source_key="factor.calculation", limit=10
    )
    assert {r.batch_id for r in bars} == {"filter-a", "filter-c"}
    assert {r.batch_id for r in factors} == {"filter-b"}


# ---------------------------------------------------------------------------
# 8. audit_batch_atomicity match
# ---------------------------------------------------------------------------


def test_audit_batch_atomicity_match(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    begin_batch(
        warehouse,
        batch_id="atomic-match",
        source_key="ingest.daily_bars.business",
    )
    for index in range(100):
        _insert_raw_daily_bar(
            warehouse,
            batch_id="atomic-match",
            trade_date=date(2026, 7, 1) + timedelta(days=index),
            symbol=f"600{index:03d}",
        )
    finalize_batch(
        warehouse,
        batch_id="atomic-match",
        status="committed",
        rows_written=100,
        rows_received=100,
    )
    entry = audit_batch_atomicity(warehouse, batch_id="atomic-match")
    assert entry is not None
    assert entry.actual_rows_in_table == 100
    assert entry.atomicity_evidence["match"] is True
    assert entry.atomicity_evidence["expected"] == 100
    assert entry.atomicity_evidence["actual"] == 100
    assert entry.table_name == "raw_daily_bars"


# ---------------------------------------------------------------------------
# 9. audit_batch_atomicity no match
# ---------------------------------------------------------------------------


def test_audit_batch_atomicity_mismatch(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    begin_batch(
        warehouse,
        batch_id="atomic-mismatch",
        source_key="ingest.daily_bars.business",
    )
    for index in range(80):
        _insert_raw_daily_bar(
            warehouse,
            batch_id="atomic-mismatch",
            trade_date=date(2026, 7, 1) + timedelta(days=index),
            symbol=f"601{index:03d}",
        )
    finalize_batch(
        warehouse,
        batch_id="atomic-mismatch",
        status="committed",
        rows_written=100,
        rows_received=100,
    )
    entry = audit_batch_atomicity(warehouse, batch_id="atomic-mismatch")
    assert entry is not None
    assert entry.actual_rows_in_table == 80
    assert entry.atomicity_evidence["match"] is False
    assert entry.atomicity_evidence["expected"] == 100
    assert entry.atomicity_evidence["actual"] == 80


# ---------------------------------------------------------------------------
# 10. audit_batch_atomicity batch not found
# ---------------------------------------------------------------------------


def test_audit_batch_atomicity_returns_none_for_missing(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    entry = audit_batch_atomicity(warehouse, batch_id="does-not-exist")
    assert entry is None


# ---------------------------------------------------------------------------
# 11. diagnose_factor_lag no lag
# ---------------------------------------------------------------------------


def test_diagnose_factor_lag_no_lag(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    same_date = date(2026, 7, 31)
    _insert_raw_daily_bar(
        warehouse, batch_id="lag-batch", trade_date=same_date
    )
    _insert_factor_value(
        warehouse, calc_batch_id="lag-factors", trade_date=same_date
    )
    begin_batch(
        warehouse,
        batch_id="lag-factors",
        source_key="factor.calculation",
    )
    finalize_batch(
        warehouse,
        batch_id="lag-factors",
        status="committed",
        rows_written=1,
    )
    diag = diagnose_factor_lag(warehouse)
    assert diag.lag_days == 0
    assert diag.is_lagging is False
    assert diag.severity == "none"
    assert diag.raw_daily_bars_latest_date is not None
    assert diag.factor_values_latest_date is not None


# ---------------------------------------------------------------------------
# 12. diagnose_factor_lag minor (1 day)
# ---------------------------------------------------------------------------


def test_diagnose_factor_lag_minor(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    _insert_raw_daily_bar(
        warehouse, batch_id="lag-batch", trade_date=date(2026, 8, 1)
    )
    _insert_factor_value(
        warehouse,
        calc_batch_id="lag-factors",
        trade_date=date(2026, 7, 31),
    )
    begin_batch(
        warehouse,
        batch_id="lag-factors",
        source_key="factor.calculation",
    )
    finalize_batch(
        warehouse,
        batch_id="lag-factors",
        status="committed",
        rows_written=1,
    )
    diag = diagnose_factor_lag(warehouse)
    assert diag.lag_days == 1
    assert diag.is_lagging is True
    assert diag.severity == "minor"
    assert "rerun_factor_calculation" in diag.recommended_actions


# ---------------------------------------------------------------------------
# 13. diagnose_factor_lag critical (10 days)
# ---------------------------------------------------------------------------


def test_diagnose_factor_lag_critical(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    _insert_raw_daily_bar(
        warehouse, batch_id="lag-batch", trade_date=date(2026, 8, 10)
    )
    _insert_factor_value(
        warehouse,
        calc_batch_id="lag-factors",
        trade_date=date(2026, 7, 31),
    )
    begin_batch(
        warehouse,
        batch_id="lag-factors",
        source_key="factor.calculation",
    )
    finalize_batch(
        warehouse,
        batch_id="lag-factors",
        status="committed",
        rows_written=1,
    )
    diag = diagnose_factor_lag(warehouse)
    assert diag.lag_days == 10
    assert diag.is_lagging is True
    assert diag.severity == "critical"
    assert "rerun_factor_calculation" in diag.recommended_actions
    assert "check_factor_pipeline_failures" in diag.recommended_actions
    assert "alert_data_ops" in diag.recommended_actions


# ---------------------------------------------------------------------------
# 14. diagnose_factor_lag no data
# ---------------------------------------------------------------------------


def test_diagnose_factor_lag_empty_warehouse(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    diag = diagnose_factor_lag(warehouse)
    assert diag.raw_daily_bars_latest_date is None
    assert diag.factor_values_latest_date is None
    assert diag.lag_days == 0
    assert diag.is_lagging is False
    assert diag.severity == "none"
    assert diag.raw_daily_bars_count == 0
    assert diag.factor_values_count == 0
    assert "sync_raw_data" in diag.recommended_actions
    assert "run_factor_pipeline" in diag.recommended_actions


# ---------------------------------------------------------------------------
# 15. get_batch_audit_report complete
# ---------------------------------------------------------------------------


def test_get_batch_audit_report_complete(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    begin_batch(
        warehouse,
        batch_id="report-1",
        source_key="ingest.daily_bars.business",
        scope={"adjust": "qfq"},
    )
    finalize_batch(
        warehouse,
        batch_id="report-1",
        status="committed",
        rows_written=2,
        rows_received=2,
    )
    begin_batch(
        warehouse,
        batch_id="report-2",
        source_key="factor.calculation",
    )
    finalize_batch(
        warehouse,
        batch_id="report-2",
        status="failed",
        error={"error_type": "RuntimeError", "error_message": "oops"},
    )
    _insert_raw_daily_bar(
        warehouse, batch_id="report-1", trade_date=date(2026, 7, 31)
    )
    _insert_raw_daily_bar(
        warehouse,
        batch_id="report-1",
        trade_date=date(2026, 7, 30),
        symbol="000001",
    )

    report = get_batch_audit_report(warehouse, recent_batch_limit=10)
    assert report.warehouse_available is True
    assert len(report.batches) == 2
    assert report.summary["total_batches"] == 2
    assert report.summary["committed"] == 1
    assert report.summary["failed"] == 1
    assert isinstance(report.lag_diagnostic, BatchLagDiagnostic)
    assert "raw_daily_bars" in report.source_table_stats
    assert "factor_values" in report.source_table_stats

    payload = report.to_dict()
    assert "batches" in payload
    assert "lag_diagnostic" in payload
    assert "summary" in payload

    match_entry = next(
        e for e in report.batches if e.batch.batch_id == "report-1"
    )
    assert match_entry.actual_rows_in_table == 2
    assert match_entry.atomicity_evidence["match"] is True


# ---------------------------------------------------------------------------
# 16. batch_context finalize error is swallowed
# ---------------------------------------------------------------------------


def test_batch_context_swallows_finalize_error(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    with patch(
        "app.services.factors.batch_audit.finalize_batch",
        side_effect=RuntimeError("duckdb explosion"),
    ):
        with batch_context(
            warehouse,
            batch_id="ctx-finalize-fail",
            source_key="factor.calculation",
        ):
            pass
    records = list_recent_batches(warehouse, limit=10)
    assert len(records) == 1
    assert records[0].status == "running"


# ---------------------------------------------------------------------------
# 17. batch_context begin error is swallowed
# ---------------------------------------------------------------------------


def test_batch_context_swallows_begin_error(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    with patch(
        "app.services.factors.batch_audit.begin_batch",
        side_effect=RuntimeError("duckdb locked"),
    ):
        with batch_context(
            warehouse,
            batch_id="ctx-begin-fail",
            source_key="factor.calculation",
        ):
            pass
    records = list_recent_batches(warehouse, limit=10)
    assert records == []


# ---------------------------------------------------------------------------
# 18. audit_batch_atomicity for factor_values (calc_batch_id)
# ---------------------------------------------------------------------------


def test_audit_batch_atomicity_factor_values(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    begin_batch(
        warehouse,
        batch_id="fv-batch",
        source_key="factor.calculation",
    )
    for index in range(5):
        _insert_factor_value(
            warehouse,
            calc_batch_id="fv-batch",
            trade_date=date(2026, 7, 1) + timedelta(days=index),
        )
    finalize_batch(
        warehouse,
        batch_id="fv-batch",
        status="committed",
        rows_written=5,
        rows_received=5,
    )
    entry = audit_batch_atomicity(warehouse, batch_id="fv-batch")
    assert entry is not None
    assert entry.table_name == "factor_values"
    assert entry.actual_rows_in_table == 5
    assert entry.atomicity_evidence["match"] is True
    assert entry.atomicity_evidence["batch_column"] == "calc_batch_id"


# ---------------------------------------------------------------------------
# 19. mirror_daily_bars integration: writes ingestion_batches on success
# ---------------------------------------------------------------------------


def test_mirror_daily_bars_records_committed_batch(db_session, tmp_path):
    """Verify mirror_daily_bars writes a 'committed' batch to
    ingestion_batches and the atomicity check matches."""
    from app.models.daily_bar import DailyBar
    from app.models.symbol import Symbol
    from app.services.factors.bar_mirror import mirror_daily_bars

    symbol = Symbol(
        symbol="000001",
        name="平安银行",
        asset_type="stock",
        market="sz",
    )
    db_session.add(symbol)
    db_session.flush()
    db_session.add(
        DailyBar(
            symbol_id=symbol.id,
            trade_date=date(2026, 7, 10),
            open=10,
            high=11,
            low=9,
            close=10,
            source="test",
        )
    )
    db_session.commit()

    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    result = mirror_daily_bars(
        db_session,
        warehouse=warehouse,
        include_universe=False,
        batch_size=1,
    )

    records = list_recent_batches(warehouse, limit=10)
    assert len(records) == 1
    assert records[0].batch_id == result.batch_id
    assert records[0].source_key == "ingest.daily_bars.business"
    assert records[0].status == "committed"
    assert records[0].rows_written == result.rows_written
    assert records[0].finished_at is not None

    entry = audit_batch_atomicity(warehouse, batch_id=result.batch_id)
    assert entry is not None
    assert entry.actual_rows_in_table == result.rows_written
    assert entry.atomicity_evidence["match"] is True


# ---------------------------------------------------------------------------
# 20. mirror_daily_bars integration: records failed batch on error
# ---------------------------------------------------------------------------


def test_mirror_daily_bars_records_failed_batch_on_error(
    db_session, tmp_path, monkeypatch
):
    """Verify mirror_daily_bars writes a 'failed' batch when the mirror
    raises, and the original exception still propagates."""
    from app.models.daily_bar import DailyBar
    from app.models.symbol import Symbol
    from app.services.factors.bar_mirror import mirror_daily_bars

    symbol = Symbol(
        symbol="000002",
        name="万科A",
        asset_type="stock",
        market="sz",
    )
    db_session.add(symbol)
    db_session.flush()
    db_session.add(
        DailyBar(
            symbol_id=symbol.id,
            trade_date=date(2026, 7, 10),
            open=10,
            high=11,
            low=9,
            close=10,
            source="test",
        )
    )
    db_session.commit()

    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    original_upsert = warehouse.upsert_daily_bars

    def fail_upsert(*args, **kwargs):
        raise RuntimeError("injected DuckDB failure")

    monkeypatch.setattr(warehouse, "upsert_daily_bars", fail_upsert)

    with pytest.raises(RuntimeError, match="injected"):
        mirror_daily_bars(
            db_session,
            warehouse=warehouse,
            include_universe=False,
            batch_size=1,
        )

    records = list_recent_batches(warehouse, limit=10)
    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].error_json is not None
    error = json.loads(records[0].error_json)
    assert "injected DuckDB failure" in error["error_message"]
