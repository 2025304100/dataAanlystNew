"""Add durable external-data sync plans and retryable partitions.

Revision ID: wps_0029_029_data_sync_plans
Revises: wps_0028_028_portfolio_asset_scope
"""

from alembic import op
import sqlalchemy as sa


revision = "wps_0029_029_data_sync_plans"
down_revision = "wps_0028_028_portfolio_asset_scope"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "data_sync_plans" not in tables:
        op.create_table(
            "data_sync_plans",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("parent_plan_id", sa.String(length=64), nullable=True),
            sa.Column("dataset", sa.String(length=32), nullable=False),
            sa.Column("mode", sa.String(length=16), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
            sa.Column("requested_start_date", sa.Date(), nullable=False),
            sa.Column("requested_end_date", sa.Date(), nullable=False),
            sa.Column("partition_strategy", sa.String(length=32), nullable=False),
            sa.Column("total_partitions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed_partitions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped_partitions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_partitions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["parent_plan_id"], ["data_sync_plans.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_data_sync_plans_task_id", "data_sync_plans", ["task_id"])
        op.create_index("ix_data_sync_plans_parent_plan_id", "data_sync_plans", ["parent_plan_id"])
        op.create_index("ix_data_sync_plans_dataset", "data_sync_plans", ["dataset"])
        op.create_index("ix_data_sync_plans_mode", "data_sync_plans", ["mode"])
        op.create_index("ix_data_sync_plans_status", "data_sync_plans", ["status"])
        op.create_index("ix_data_sync_plans_created_at", "data_sync_plans", ["created_at"])

    inspector = sa.inspect(op.get_bind())
    if "data_sync_partitions" not in inspector.get_table_names():
        op.create_table(
            "data_sync_partitions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("plan_id", sa.String(length=64), nullable=False),
            sa.Column("partition_key", sa.String(length=160), nullable=False),
            sa.Column("symbol_id", sa.Integer(), nullable=True),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rows_written", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["plan_id"], ["data_sync_plans.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("plan_id", "partition_key", name="uq_data_sync_partition_key"),
        )
        op.create_index("ix_data_sync_partitions_plan_id", "data_sync_partitions", ["plan_id"])
        op.create_index("ix_data_sync_partitions_symbol_id", "data_sync_partitions", ["symbol_id"])
        op.create_index("ix_data_sync_partitions_status", "data_sync_partitions", ["status"])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "data_sync_partitions" in tables:
        op.drop_table("data_sync_partitions")
    if "data_sync_plans" in tables:
        op.drop_table("data_sync_plans")
