"""Align the durable data-quality schema with the runtime governance contract.

The original ``data_quality_snapshots`` table predates the field-level quality
service.  Existing rows are retained and explicitly marked ``legacy`` while
the current ORM columns are added.  The audit-table CHECK is rebuilt because
SQLite cannot alter a CHECK constraint in place.

Revision ID: wps_0023_036_data_quality_snapshot_contract
Revises: wps_0023_035_data_quality_quarantines
Create Date: 2026-08-21 18:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_036_data_quality_snapshot_contract"
down_revision = "wps_0023_035_data_quality_quarantines"
branch_labels = None
depends_on = None


QUALITY_TABLE = "data_quality_snapshots"
AUDIT_TABLE = "data_governance_audit_events"

AUDIT_ACTIONS = (
    "PORTFOLIO_CANDIDATE_SCD2_CHANGE",
    "BENCHMARK_SOURCE_FAILOVER",
    "AUTO_SIMULATION_RESULT",
    "RECONCILIATION_RESULT",
    "ILLEGAL_STATE_TRANSITION",
    "FACTOR_USAGE_APPLIED",
    "OUTBOX_EVENT_DISPATCHED",
    "DATA_BLOCK_RESOLUTION",
    "DATA_SOURCE_FAILOVER",
    "DATA_QUALITY_QUARANTINE",
    "UNKNOWN_AUDIT_ACTION",
)
AUDIT_CHECK_SQL = "action IN ({})".format(
    ",".join("'{}'".format(action) for action in AUDIT_ACTIONS)
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(name: str) -> bool:
    return name in _inspector().get_table_names()


def _columns(table: str) -> dict[str, dict]:
    return {item["name"]: item for item in _inspector().get_columns(table)}


def _quality_columns(*, upgrade_legacy: bool) -> tuple[sa.Column, ...]:
    """Return the current ORM shape, with safe defaults for existing rows."""
    legacy_text_default = sa.text("'legacy'") if upgrade_legacy else None
    zero_default = sa.text("0") if upgrade_legacy else None
    captured_default = sa.text("CURRENT_TIMESTAMP") if upgrade_legacy else None
    return (
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(32), nullable=False, server_default=legacy_text_default),
        sa.Column("field", sa.String(64), nullable=False, server_default=legacy_text_default),
        sa.Column("readiness", sa.String(16), nullable=False, server_default=legacy_text_default),
        sa.Column("evaluation_mode", sa.String(16), nullable=False, server_default=legacy_text_default),
        sa.Column("row_count", sa.Integer, nullable=False, server_default=zero_default),
        sa.Column("nonnull_rows", sa.Integer, nullable=False, server_default=zero_default),
        sa.Column("distinct_symbols", sa.Integer, nullable=False, server_default=zero_default),
        sa.Column("distinct_dates", sa.Integer, nullable=False, server_default=zero_default),
        sa.Column("first_date", sa.String(16), nullable=True),
        sa.Column("latest_date", sa.String(16), nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("metrics_json", sa.Text, nullable=True),
        sa.Column("quality_level", sa.String(8), nullable=True),
        sa.Column("quality_assessment_id", sa.String(64), nullable=True),
        sa.Column("missing_ratio", sa.Float, nullable=True),
        sa.Column("max_consecutive_gap_days", sa.Integer, nullable=True),
        sa.Column("quarantine_required", sa.Boolean, nullable=True),
        sa.Column("captured_at", sa.DateTime, nullable=False, server_default=captured_default),
    )


def _is_string_type(type_: sa.types.TypeEngine) -> bool:
    return isinstance(type_, sa.String)


def _legacy_columns_need_relaxing(columns: dict[str, dict]) -> bool:
    # The current runtime service does not write these pre-Stage-2 fields.
    # They must be optional after upgrade, or every new snapshot fails before
    # the quality gate can store its evidence.
    return any(
        name in columns and not bool(columns[name].get("nullable"))
        for name in (
            "snapshot_date",
            "data_type",
            "total_count",
            "valid_count",
            "invalid_count",
            "missing_count",
            "created_at",
        )
    )


def _ensure_quality_snapshot_contract() -> None:
    if not _table_exists(QUALITY_TABLE):
        op.create_table(QUALITY_TABLE, *_quality_columns(upgrade_legacy=False))
        _ensure_quality_indexes()
        return

    columns = _columns(QUALITY_TABLE)
    expected = {column.name: column for column in _quality_columns(upgrade_legacy=True)}
    missing = [column for name, column in expected.items() if name not in columns and name != "id"]
    id_is_legacy_integer = "id" in columns and not _is_string_type(columns["id"]["type"])
    needs_rebuild = id_is_legacy_integer or _legacy_columns_need_relaxing(columns)

    if needs_rebuild:
        with op.batch_alter_table(QUALITY_TABLE, recreate="always") as batch_op:
            if id_is_legacy_integer:
                batch_op.alter_column(
                    "id",
                    existing_type=columns["id"]["type"],
                    type_=sa.String(64),
                    existing_nullable=bool(columns["id"].get("nullable")),
                )
            for name in (
                "snapshot_date",
                "data_type",
                "total_count",
                "valid_count",
                "invalid_count",
                "missing_count",
                "created_at",
            ):
                column = columns.get(name)
                if column is not None and not bool(column.get("nullable")):
                    batch_op.alter_column(
                        name,
                        existing_type=column["type"],
                        nullable=True,
                    )
            for column in missing:
                batch_op.add_column(column)
    else:
        for column in missing:
            op.add_column(QUALITY_TABLE, column)

    # The old table contains aggregate snapshots.  Preserve those rows but do
    # not claim they are equivalent to the new field-level/PIT assessments.
    # Default values make this safe for an empty table as well.
    columns = _columns(QUALITY_TABLE)
    assignments: list[str] = []
    if "data_type" in columns:
        assignments.append(
            "dataset = COALESCE(NULLIF(data_type, ''), dataset, 'legacy')"
        )
    if "scope" in columns and "data_type" in columns:
        assignments.append(
            "field = COALESCE(NULLIF(scope, ''), NULLIF(data_type, ''), field, 'legacy')"
        )
    elif "data_type" in columns:
        assignments.append("field = COALESCE(NULLIF(data_type, ''), field, 'legacy')")
    if "total_count" in columns:
        assignments.append("row_count = COALESCE(total_count, row_count, 0)")
    if "valid_count" in columns:
        assignments.append("nonnull_rows = COALESCE(valid_count, nonnull_rows, 0)")
    if "created_at" in columns:
        assignments.append("captured_at = COALESCE(created_at, captured_at, CURRENT_TIMESTAMP)")
    if assignments:
        op.execute(sa.text("UPDATE {} SET {}".format(QUALITY_TABLE, ", ".join(assignments))))

    _ensure_quality_indexes()


def _ensure_quality_indexes() -> None:
    existing = {item.get("name") for item in _inspector().get_indexes(QUALITY_TABLE)}
    for name, columns in (
        ("ix_data_quality_snapshots_dataset", ["dataset"]),
        ("ix_data_quality_snapshots_field", ["field"]),
        ("ix_data_quality_snapshots_readiness", ["readiness"]),
        ("ix_data_quality_snapshots_quality_level", ["quality_level"]),
        ("ix_data_quality_snapshots_quality_assessment_id", ["quality_assessment_id"]),
        ("ix_data_quality_snapshots_captured_at", ["captured_at"]),
    ):
        if name not in existing:
            op.create_index(name, QUALITY_TABLE, columns, unique=False)


def _audit_check_needs_refresh() -> bool:
    if not _table_exists(AUDIT_TABLE):
        return False
    checks = _inspector().get_check_constraints(AUDIT_TABLE)
    matching = [
        item for item in checks
        if item.get("name") == "ck_dg_audit_action_values"
    ]
    if not matching:
        return True
    expression = str(matching[0].get("sqltext") or "")
    return not all("'{}'".format(action) in expression for action in AUDIT_ACTIONS)


def _ensure_audit_action_contract() -> None:
    if not _audit_check_needs_refresh():
        return
    checks = _inspector().get_check_constraints(AUDIT_TABLE)
    named_check = next(
        (item.get("name") for item in checks if item.get("name") == "ck_dg_audit_action_values"),
        None,
    )
    if op.get_bind().dialect.name == "sqlite":
        # SQLite has no ALTER TABLE DROP CONSTRAINT support.
        with op.batch_alter_table(AUDIT_TABLE, recreate="always") as batch_op:
            if named_check:
                batch_op.drop_constraint(named_check, type_="check")
            batch_op.create_check_constraint("ck_dg_audit_action_values", AUDIT_CHECK_SQL)
        return

    if named_check:
        op.drop_constraint(named_check, AUDIT_TABLE, type_="check")
    op.create_check_constraint("ck_dg_audit_action_values", AUDIT_TABLE, AUDIT_CHECK_SQL)


def upgrade() -> None:
    _ensure_quality_snapshot_contract()
    _ensure_audit_action_contract()


def downgrade() -> None:
    # Do not drop new columns or tighten the action constraint: both would
    # make already-persisted governance evidence unreadable to older release
    # tooling.  This forward-compatible migration is intentionally no-op on
    # downgrade.
    pass
