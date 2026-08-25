"""Persist immutable data-quality quarantine observations.

The application model was introduced after the original data-sync revisions,
so this migration deliberately tolerates both database shapes: installations
with ``data_sync_partitions`` get a foreign key, while older installations
without that table still receive the quarantine ledger (with a nullable
partition reference) instead of failing the whole upgrade.
"""
from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, inspect


revision = "wps_0023_035_data_quality_quarantines"
down_revision = "wps_0023_034_portfolio_rule_exit_config"
branch_labels = None
depends_on = None


TABLE = "data_quality_quarantines"


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _table_exists(TABLE):
        return

    bind = op.get_bind()
    has_partition_table = _table_exists("data_sync_partitions")
    partition_column = (
        Column(
            "partition_id",
            String(64),
            ForeignKey("data_sync_partitions.id", ondelete="SET NULL"),
            nullable=True,
        )
        if has_partition_table
        else Column("partition_id", String(64), nullable=True)
    )

    op.create_table(
        TABLE,
        Column("id", String(64), primary_key=True),
        partition_column,
        Column("assessment_id", String(64), nullable=True),
        Column("dataset", String(64), nullable=False),
        Column("field", String(128), nullable=True),
        Column("symbol", String(64), nullable=True),
        Column("status", String(16), nullable=False, server_default="quarantined"),
        Column("reason_code", String(64), nullable=False),
        Column("reason", Text, nullable=True),
        Column("raw_payload_json", Text, nullable=True),
        Column("raw_payload_hash", String(128), nullable=True),
        Column("source_name", String(64), nullable=True),
        Column("source_version", String(128), nullable=True),
        Column("correlation_id", String(128), nullable=True),
        Column("created_at", DateTime, nullable=False),
        Column("released_at", DateTime, nullable=True),
    )
    for name, columns in (
        ("ix_dq_quarantine_dataset_status", ["dataset", "status"]),
        ("ix_dq_quarantine_partition", ["partition_id"]),
        ("ix_dq_quarantine_correlation", ["correlation_id"]),
        ("ix_data_quality_quarantines_assessment_id", ["assessment_id"]),
        ("ix_data_quality_quarantines_dataset", ["dataset"]),
        ("ix_data_quality_quarantines_symbol", ["symbol"]),
        ("ix_data_quality_quarantines_status", ["status"]),
        ("ix_data_quality_quarantines_created_at", ["created_at"]),
    ):
        op.create_index(name, TABLE, columns, unique=False)


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    inspector = inspect(op.get_bind())
    for index in inspector.get_indexes(TABLE):
        name = index.get("name")
        if name:
            try:
                op.drop_index(name, table_name=TABLE)
            except Exception:
                pass
    op.drop_table(TABLE)
