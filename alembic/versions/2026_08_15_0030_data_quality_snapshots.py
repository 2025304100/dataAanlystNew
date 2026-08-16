"""Add persisted formula-input quality snapshots.

Revision ID: wps_0030_030_data_quality_snapshots
Revises: wps_0029_029_data_sync_plans
"""
from alembic import op
import sqlalchemy as sa


revision = "wps_0030_030_data_quality_snapshots"
down_revision = "wps_0029_029_data_sync_plans"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "data_quality_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "data_quality_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("dataset", sa.String(length=32), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("readiness", sa.String(length=16), nullable=False),
        sa.Column("evaluation_mode", sa.String(length=16), nullable=False, server_default="continuous"),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("nonnull_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_symbols", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_dates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_date", sa.String(length=16), nullable=True),
        sa.Column("latest_date", sa.String(length=16), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    for name, columns in (
        ("ix_data_quality_snapshots_dataset", ["dataset"]),
        ("ix_data_quality_snapshots_field", ["field"]),
        ("ix_data_quality_snapshots_readiness", ["readiness"]),
        ("ix_data_quality_snapshots_captured_at", ["captured_at"]),
    ):
        op.create_index(name, "data_quality_snapshots", columns)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "data_quality_snapshots" in inspector.get_table_names():
        op.drop_table("data_quality_snapshots")
