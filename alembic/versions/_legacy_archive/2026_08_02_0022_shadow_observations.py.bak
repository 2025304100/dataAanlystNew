"""WP6-03: shadow observations table.

Creates factor_shadow_observations table for daily Shadow factor observations.
Idempotent: safe to run multiple times.

Revision ID: wps_0023_022_shadow_observations
Revises: wps_0023_021_factor_library_lifecycle
Create Date: 2026-08-02

对齐 docs/专业因子库开发计划.md §WP6-03 和 docs/因子设置与专业因子库改造方案.md §9.3。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_022_shadow_observations"
down_revision: Union[str, None] = "wps_0023_021_factor_library_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "factor_shadow_observations"


def upgrade() -> None:
    """Create factor_shadow_observations table (idempotent)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if TABLE_NAME in existing_tables:
        # Table already exists; idempotent no-op
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("factor_id", sa.Integer(), nullable=False),
        sa.Column("factor_version_id", sa.Integer(), nullable=False),
        sa.Column("trade_date", sa.String(length=10), nullable=False),
        sa.Column("observed_symbols", sa.Integer(), nullable=True),
        sa.Column("expected_symbols", sa.Integer(), nullable=True),
        sa.Column("completeness_ratio", sa.Float(), nullable=True),
        sa.Column("is_valid_day", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("invalid_reason", sa.String(length=64), nullable=True),
        sa.Column("ic_value", sa.Float(), nullable=True),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("turnover", sa.Float(), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True, server_default="{}"),
        sa.Column("health_status", sa.String(length=24), nullable=False, server_default="healthy"),
        sa.Column("health_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["factor_id"], ["factors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["factor_version_id"], ["factor_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "factor_version_id", "trade_date",
            name="uq_shadow_observation_version_date",
        ),
    )

    op.create_index(
        "ix_shadow_observations_factor_id",
        TABLE_NAME,
        ["factor_id"],
    )
    op.create_index(
        "ix_shadow_observations_factor_version_id",
        TABLE_NAME,
        ["factor_version_id"],
    )
    op.create_index(
        "ix_shadow_observations_trade_date",
        TABLE_NAME,
        ["trade_date"],
    )
    op.create_index(
        "ix_shadow_obs_factor_trade",
        TABLE_NAME,
        ["factor_id", "trade_date"],
    )


def downgrade() -> None:
    """Drop factor_shadow_observations table (idempotent)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in inspector.get_table_names():
        # Table already dropped; idempotent no-op
        return

    existing_indexes = {i["name"] for i in inspector.get_indexes(TABLE_NAME)}
    for idx_name in (
        "ix_shadow_obs_factor_trade",
        "ix_shadow_observations_trade_date",
        "ix_shadow_observations_factor_version_id",
        "ix_shadow_observations_factor_id",
    ):
        if idx_name in existing_indexes:
            op.drop_index(idx_name, table_name=TABLE_NAME)

    op.drop_table(TABLE_NAME)
