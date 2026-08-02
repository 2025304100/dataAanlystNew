"""WPD-06: Batch audit and factor lag diagnostics for the DuckDB warehouse.

Provides begin/finalize batch lifecycle hooks, atomicity verification, and
factor_values lag detection. All batch audit operations are best-effort and
must never block the main ingestion/calculation pipeline.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterator, Literal

from app.services.factors.store import FactorWarehouse

logger = logging.getLogger(__name__)

BatchStatus = Literal["running", "committed", "failed"]
BatchSourceKey = Literal[
    "ingest.daily_bars.business",
    "ingest.daily_bars.universe",
    "ingest.valuation_snapshots",
    "ingest.financial_reports",
    "ingest.fund_flows",
    "ingest.sentiment",
    "ingest.tail_proxy",
    "ingest.macro",
    "ingest.universe_metadata",
    "factor.calculation",
    "target.generation",
]

# source_key → actual data table name
SOURCE_KEY_TO_TABLE: dict[str, str] = {
    "ingest.daily_bars.business": "raw_daily_bars",
    "ingest.daily_bars.universe": "raw_daily_bars",
    "ingest.valuation_snapshots": "raw_valuation_snapshots",
    "ingest.financial_reports": "raw_financial_reports",
    "ingest.fund_flows": "raw_fund_flows",
    "ingest.sentiment": "raw_sentiment",
    "ingest.tail_proxy": "raw_tail_proxy",
    "ingest.macro": "raw_macro",
    "ingest.universe_metadata": "raw_asset_universe",
    "factor.calculation": "factor_values",
    "target.generation": "factor_targets",
}

# raw_* tables that track rows via a `batch_id` column
_BATCH_ID_TABLES = frozenset({
    "raw_daily_bars",
    "raw_valuation_snapshots",
    "raw_financial_reports",
    "raw_fund_flows",
    "raw_sentiment",
    "raw_tail_proxy",
    "raw_macro",
})
# factor tables that track rows via a `calc_batch_id` column
_CALC_BATCH_ID_TABLES = frozenset({"factor_values", "factor_targets"})

_BATCH_SELECT_COLUMNS = (
    "batch_id, source_key, scope_json, status, started_at, "
    "finished_at, rows_received, rows_written, error_json, "
    "source_contract_version"
)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_date(value: Any) -> date | None:
    """Coerce a DuckDB DATE/TIMESTAMP/string value into a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        text = str(value)
        return date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def _batch_column_for_table(table_name: str) -> str | None:
    if table_name in _BATCH_ID_TABLES:
        return "batch_id"
    if table_name in _CALC_BATCH_ID_TABLES:
        return "calc_batch_id"
    return None


def _row_to_batch_record(row: Any) -> BatchRecord:
    return BatchRecord(
        batch_id=str(row[0]),
        source_key=str(row[1]),
        scope_json=row[2],
        status=str(row[3]),
        started_at=row[4],
        finished_at=row[5],
        rows_received=int(row[6] or 0),
        rows_written=int(row[7] or 0),
        error_json=row[8],
        source_contract_version=row[9],
    )


@dataclass(frozen=True)
class BatchRecord:
    """ingestion_batches table record."""
    batch_id: str
    source_key: str
    scope_json: str | None
    status: BatchStatus
    started_at: datetime
    finished_at: datetime | None
    rows_received: int
    rows_written: int
    error_json: str | None
    source_contract_version: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "source_key": self.source_key,
            "scope": json.loads(self.scope_json) if self.scope_json else None,
            "scope_json": self.scope_json,
            "status": self.status,
            "started_at": self.started_at.isoformat()
            if self.started_at
            else None,
            "finished_at": self.finished_at.isoformat()
            if self.finished_at
            else None,
            "rows_received": self.rows_received,
            "rows_written": self.rows_written,
            "error": json.loads(self.error_json) if self.error_json else None,
            "error_json": self.error_json,
            "source_contract_version": self.source_contract_version,
        }


@dataclass(frozen=True)
class BatchAuditEntry:
    """Batch audit entry with atomicity evidence."""
    batch: BatchRecord
    actual_rows_in_table: int
    table_name: str
    atomicity_evidence: dict[str, Any]
    age_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch": self.batch.to_dict(),
            "actual_rows_in_table": self.actual_rows_in_table,
            "table_name": self.table_name,
            "atomicity_evidence": self.atomicity_evidence,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True)
class BatchLagDiagnostic:
    """factor_values lag diagnostic."""
    raw_daily_bars_latest_date: str | None
    factor_values_latest_date: str | None
    lag_days: int
    raw_daily_bars_count: int
    factor_values_count: int
    latest_factor_batch_id: str | None
    latest_factor_batch_status: BatchStatus | None
    is_lagging: bool
    severity: Literal["none", "minor", "major", "critical"]
    recommended_actions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BatchAuditReport:
    """Batch audit report."""
    generated_at: str
    warehouse_available: bool
    warehouse_path: str
    batches: list[BatchAuditEntry]
    lag_diagnostic: BatchLagDiagnostic
    source_table_stats: dict[str, dict]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "warehouse_available": self.warehouse_available,
            "warehouse_path": self.warehouse_path,
            "batches": [entry.to_dict() for entry in self.batches],
            "lag_diagnostic": self.lag_diagnostic.to_dict(),
            "source_table_stats": self.source_table_stats,
            "summary": self.summary,
        }


def begin_batch(
    warehouse: FactorWarehouse,
    *,
    batch_id: str,
    source_key: BatchSourceKey,
    scope: dict[str, Any] | None = None,
    source_contract_version: str | None = None,
) -> None:
    """Insert a status='running' record into ingestion_batches.

    Must be called within a warehouse write context. Raises on DuckDB errors
    so callers can decide to swallow (best-effort) or propagate.
    """
    scope_json = (
        json.dumps(scope, ensure_ascii=False, default=str)
        if scope
        else None
    )
    started_at = _utcnow_naive()
    warehouse.initialize()
    with warehouse._write_lock, warehouse.connection() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                "INSERT INTO ingestion_batches "
                "(batch_id, source_key, scope_json, status, started_at, "
                " finished_at, rows_received, rows_written, error_json, "
                " source_contract_version) "
                "VALUES (?, ?, ?, 'running', ?, NULL, 0, 0, NULL, ?)",
                [
                    batch_id,
                    source_key,
                    scope_json,
                    started_at,
                    source_contract_version,
                ],
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise


def finalize_batch(
    warehouse: FactorWarehouse,
    *,
    batch_id: str,
    status: BatchStatus,
    rows_received: int = 0,
    rows_written: int = 0,
    error: dict[str, Any] | None = None,
) -> None:
    """Update an ingestion_batches row to its final state.

    If status='failed', error should be populated; if status='committed',
    rows_written should reflect the rows persisted to the data table.
    """
    if status == "failed" and error is None:
        error = {"error": "unknown_failure"}
    error_json = (
        json.dumps(error, ensure_ascii=False, default=str)
        if error
        else None
    )
    finished_at = _utcnow_naive()
    warehouse.initialize()
    with warehouse._write_lock, warehouse.connection() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                "UPDATE ingestion_batches "
                "SET status = ?, finished_at = ?, rows_received = ?, "
                "    rows_written = ?, error_json = ? "
                "WHERE batch_id = ?",
                [
                    status,
                    finished_at,
                    int(rows_received),
                    int(rows_written),
                    error_json,
                    batch_id,
                ],
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise


def list_recent_batches(
    warehouse: FactorWarehouse,
    *,
    source_key: str | None = None,
    limit: int = 50,
) -> list[BatchRecord]:
    """Return the most recent batch records ordered by started_at DESC."""
    warehouse.initialize()
    with warehouse.connection(read_only=True) as conn:
        if source_key is not None:
            rows = conn.execute(
                f"SELECT {_BATCH_SELECT_COLUMNS} FROM ingestion_batches "
                "WHERE source_key = ? "
                "ORDER BY started_at DESC, batch_id DESC "
                "LIMIT ?",
                [source_key, int(limit)],
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_BATCH_SELECT_COLUMNS} FROM ingestion_batches "
                "ORDER BY started_at DESC, batch_id DESC "
                "LIMIT ?",
                [int(limit)],
            ).fetchall()
    return [_row_to_batch_record(row) for row in rows]


def audit_batch_atomicity(
    warehouse: FactorWarehouse,
    *,
    batch_id: str,
) -> BatchAuditEntry | None:
    """Verify the atomicity of a single batch.

    1. Read the batch record from ingestion_batches.
    2. Count matching rows in the corresponding data table.
    3. Compare rows_written with actual_rows_in_table.
    """
    warehouse.initialize()
    with warehouse.connection(read_only=True) as conn:
        row = conn.execute(
            f"SELECT {_BATCH_SELECT_COLUMNS} FROM ingestion_batches "
            "WHERE batch_id = ?",
            [batch_id],
        ).fetchone()
        if row is None:
            return None
        record = _row_to_batch_record(row)
        table_name = SOURCE_KEY_TO_TABLE.get(record.source_key, "")
        actual_rows = _count_batch_rows(conn, table_name, batch_id)
        now = _utcnow_naive()
        age_seconds = (
            (now - record.started_at).total_seconds()
            if record.started_at
            else 0.0
        )
        batch_column = _batch_column_for_table(table_name)
        if actual_rows < 0:
            match = None
        else:
            match = actual_rows == record.rows_written
        evidence: dict[str, Any] = {
            "expected": record.rows_written,
            "actual": actual_rows,
            "match": match,
            "table": table_name,
            "batch_column": batch_column,
        }
    return BatchAuditEntry(
        batch=record,
        actual_rows_in_table=actual_rows,
        table_name=table_name,
        atomicity_evidence=evidence,
        age_seconds=round(age_seconds, 3),
    )


def _count_batch_rows(
    conn: Any, table_name: str, batch_id: str
) -> int:
    """Count rows in a data table attributed to a batch.

    Returns -1 when the table has no batch column or the query fails.
    """
    column = _batch_column_for_table(table_name)
    if not column or not table_name:
        return -1
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {column} = ?",
            [batch_id],
        ).fetchone()
        return int(row[0] or 0)
    except Exception:
        logger.exception(
            "Failed to count batch rows in %s for batch %s",
            table_name,
            batch_id,
        )
        return -1


def _build_recommended_actions(
    severity: str,
    raw_count: int,
    factor_count: int,
) -> list[str]:
    actions: list[str] = []
    if raw_count == 0:
        actions.append("sync_raw_data")
    if factor_count == 0:
        actions.append("run_factor_pipeline")
    if severity in ("minor", "major", "critical"):
        actions.append("rerun_factor_calculation")
    if severity in ("major", "critical"):
        actions.append("check_factor_pipeline_failures")
    if severity == "critical":
        actions.append("alert_data_ops")
    if not actions:
        actions.append("none")
    return actions


def diagnose_factor_lag(
    warehouse: FactorWarehouse,
) -> BatchLagDiagnostic:
    """Diagnose how far factor_values lags behind raw_daily_bars."""
    warehouse.initialize()
    with warehouse.connection(read_only=True) as conn:
        raw_row = conn.execute(
            "SELECT MAX(trade_date), COUNT(*) FROM raw_daily_bars"
        ).fetchone()
        factor_row = conn.execute(
            "SELECT MAX(trade_date), COUNT(*) FROM factor_values"
        ).fetchone()
        batch_row = conn.execute(
            "SELECT batch_id, status FROM ingestion_batches "
            "WHERE source_key = 'factor.calculation' "
            "ORDER BY started_at DESC, batch_id DESC LIMIT 1"
        ).fetchone()

    raw_latest = raw_row[0] if raw_row else None
    raw_count = int(raw_row[1] or 0) if raw_row else 0
    factor_latest = factor_row[0] if factor_row else None
    factor_count = int(factor_row[1] or 0) if factor_row else 0

    raw_date_str = str(raw_latest) if raw_latest is not None else None
    factor_date_str = (
        str(factor_latest) if factor_latest is not None else None
    )

    raw_d = _to_date(raw_latest)
    factor_d = _to_date(factor_latest)
    if raw_d is not None and factor_d is not None:
        lag_days = (raw_d - factor_d).days
    else:
        lag_days = 0

    is_lagging = lag_days > 0
    if not is_lagging:
        severity: Literal["none", "minor", "major", "critical"] = "none"
    elif lag_days <= 3:
        severity = "minor"
    elif lag_days <= 7:
        severity = "major"
    else:
        severity = "critical"

    recommended = _build_recommended_actions(severity, raw_count, factor_count)

    return BatchLagDiagnostic(
        raw_daily_bars_latest_date=raw_date_str,
        factor_values_latest_date=factor_date_str,
        lag_days=lag_days,
        raw_daily_bars_count=raw_count,
        factor_values_count=factor_count,
        latest_factor_batch_id=str(batch_row[0]) if batch_row else None,
        latest_factor_batch_status=batch_row[1] if batch_row else None,
        is_lagging=is_lagging,
        severity=severity,
        recommended_actions=recommended,
    )


def get_batch_audit_report(
    warehouse: FactorWarehouse,
    *,
    recent_batch_limit: int = 20,
) -> BatchAuditReport:
    """Build a complete batch audit report."""
    from app.services.factors.health import _raw_table_health

    generated_at = _utcnow_naive().isoformat()
    wh_health = warehouse.health()

    if not wh_health.available:
        empty_lag = BatchLagDiagnostic(
            raw_daily_bars_latest_date=None,
            factor_values_latest_date=None,
            lag_days=0,
            raw_daily_bars_count=0,
            factor_values_count=0,
            latest_factor_batch_id=None,
            latest_factor_batch_status=None,
            is_lagging=False,
            severity="none",
            recommended_actions=["initialize_warehouse"],
        )
        return BatchAuditReport(
            generated_at=generated_at,
            warehouse_available=False,
            warehouse_path=wh_health.path,
            batches=[],
            lag_diagnostic=empty_lag,
            source_table_stats={},
            summary={
                "total_batches": 0,
                "committed": 0,
                "failed": 0,
                "running": 0,
            },
        )

    warehouse.initialize()

    recent_records = list_recent_batches(
        warehouse, limit=recent_batch_limit
    )
    entries: list[BatchAuditEntry] = []
    for record in recent_records:
        entry = audit_batch_atomicity(
            warehouse, batch_id=record.batch_id
        )
        if entry is not None:
            entries.append(entry)

    lag = diagnose_factor_lag(warehouse)

    source_table_stats: dict[str, dict] = {}
    try:
        with warehouse.connection(read_only=True) as conn:
            for item in _raw_table_health(conn):
                source_table_stats[item.table] = {
                    "rows": item.row_count,
                    "latest_date": item.latest_date,
                }
    except Exception:
        logger.exception("Failed to collect raw table health")

    summary: dict[str, Any] = {
        "total_batches": len(entries),
        "committed": sum(
            1 for e in entries if e.batch.status == "committed"
        ),
        "failed": sum(
            1 for e in entries if e.batch.status == "failed"
        ),
        "running": sum(
            1 for e in entries if e.batch.status == "running"
        ),
    }

    return BatchAuditReport(
        generated_at=generated_at,
        warehouse_available=True,
        warehouse_path=wh_health.path,
        batches=entries,
        lag_diagnostic=lag,
        source_table_stats=source_table_stats,
        summary=summary,
    )


@contextmanager
def batch_context(
    warehouse: FactorWarehouse,
    *,
    batch_id: str,
    source_key: BatchSourceKey,
    scope: dict[str, Any] | None = None,
    source_contract_version: str | None = None,
) -> Iterator[None]:
    """Context manager that wraps begin/finalize_batch.

    On entry, calls begin_batch (errors are swallowed so the wrapped code
    still runs). On normal exit, calls finalize_batch(committed). On
    exception, calls finalize_batch(failed) then re-raises. Finalize errors
    are always swallowed so batch tracking never blocks the main flow.
    """
    begin_ok = False
    try:
        begin_batch(
            warehouse,
            batch_id=batch_id,
            source_key=source_key,
            scope=scope,
            source_contract_version=source_contract_version,
        )
        begin_ok = True
    except Exception:
        logger.exception(
            "begin_batch failed for %s; continuing without batch tracking",
            batch_id,
        )

    try:
        yield
    except Exception as exc:
        if begin_ok:
            try:
                finalize_batch(
                    warehouse,
                    batch_id=batch_id,
                    status="failed",
                    error={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
            except Exception:
                logger.exception(
                    "finalize_batch(failed) failed for %s", batch_id
                )
        raise
    else:
        if begin_ok:
            try:
                finalize_batch(
                    warehouse,
                    batch_id=batch_id,
                    status="committed",
                )
            except Exception:
                logger.exception(
                    "finalize_batch(committed) failed for %s", batch_id
                )


__all__ = [
    "BatchAuditEntry",
    "BatchAuditReport",
    "BatchLagDiagnostic",
    "BatchRecord",
    "BatchSourceKey",
    "BatchStatus",
    "SOURCE_KEY_TO_TABLE",
    "audit_batch_atomicity",
    "batch_context",
    "begin_batch",
    "diagnose_factor_lag",
    "finalize_batch",
    "get_batch_audit_report",
    "list_recent_batches",
]
