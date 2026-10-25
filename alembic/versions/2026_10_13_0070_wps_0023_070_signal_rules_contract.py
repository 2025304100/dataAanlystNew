"""Align the legacy signal_rules table with the current ORM without losing evidence.

Revision ID: wps_0023_070_signal_rules_contract
Revises: wps_0023_069_pc_symbol_fk_restrict

Revision 008 created rule_type/enabled/config_json, whereas the current ORM uses
the typed sample-matching columns. Keep every legacy column and its original
row values, including config_json verbatim; do NOT translate historical MACD,
custom JSON, or even similarly named JSON keys into executable new settings.
The new settings are compatibility defaults. Historical rules require manual
review before their intended semantics can be relied on.

Legacy mandatory fields become nullable so an ORM insert need not fabricate a
rule_type or config_json. Only missing new fields receive defaults. enabled is
copied to is_active only when is_active is newly added; a nullable, unfinished
is_active addition can be resumed by filling NULL alone. Existing explicit
typed values, in particular an inactive rule, are never overwritten.

Downgrade is intentionally non-destructive: retain the extensions, relaxed
legacy constraints, and all evidence. Dropping typed columns would lose rules
written after upgrade, and restoring mandatory legacy fields would require
inventing values for those rules. Re-upgrade is inspection-based and safe.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "wps_0023_070_signal_rules_contract"
down_revision = "wps_0023_069_pc_symbol_fk_restrict"
branch_labels = None
depends_on = None

_TABLE = "signal_rules"
_NEW_COLUMNS = (
    ("mode", sa.String(32), "'balanced'"),
    ("quality_tolerance", sa.Float(), "12.0"),
    ("timing_tolerance", sa.Float(), "12.0"),
    ("min_sample_count", sa.Integer(), "3"),
    ("max_samples", sa.Integer(), "60"),
    ("same_region", sa.Integer(), "1"),
    ("same_asset_type", sa.Integer(), "1"),
    ("same_stage", sa.Integer(), "1"),
    ("same_action", sa.Integer(), "1"),
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table(_TABLE):
        raise RuntimeError("signal_rules is missing: revision 008 must be applied first")
    columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
    missing = [(name, kind, default) for name, kind, default in _NEW_COLUMNS if name not in columns]
    legacy_required = [
        name for name in ("rule_type", "enabled", "config_json")
        if name in columns and not columns[name]["nullable"]
    ]
    add_active = "is_active" not in columns
    finish_active = add_active or columns["is_active"]["nullable"]

    if missing or legacy_required or add_active:
        # SQLite recreates the table for nullable alterations, preserving its
        # reflected columns/indexes. MySQL uses ordinary ALTER TABLE statements.
        with op.batch_alter_table(_TABLE) as batch:
            for name, kind, default in missing:
                batch.add_column(sa.Column(name, kind, nullable=False, server_default=sa.text(default)))
            for name in legacy_required:
                column = columns[name]
                batch.alter_column(
                    name, existing_type=column["type"], existing_nullable=False,
                    existing_server_default=(sa.text(column["default"]) if column.get("default") is not None else None),
                    nullable=True,
                )
            if add_active:
                # An intermediate NULL has a safe, distinguishable meaning if
                # non-transactional DDL stops before the enabled backfill.
                batch.add_column(sa.Column("is_active", sa.Integer(), nullable=True))

    if finish_active:
        active_default = (
            "CASE WHEN enabled IS NULL THEN 1 WHEN enabled = 0 THEN 0 ELSE 1 END"
            if "enabled" in columns else "1"
        )
        connection.execute(sa.text(
            f"UPDATE {_TABLE} SET is_active = {active_default} WHERE is_active IS NULL"
        ))
        with op.batch_alter_table(_TABLE) as batch:
            batch.alter_column(
                "is_active", existing_type=sa.Integer(), existing_nullable=True,
                nullable=False, server_default=sa.text("1"),
            )


def downgrade() -> None:
    # Preserve typed rules and the original evidence; see the module rationale.
    pass
