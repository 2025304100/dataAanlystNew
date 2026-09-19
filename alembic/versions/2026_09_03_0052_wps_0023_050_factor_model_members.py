"""Create the materialized factor-model member table.

Revision ID: wps_0023_050_factor_model_members
Revises: wps_0023_049_factor_weight_snapshots
"""
from alembic import op
import sqlalchemy as sa


revision = "wps_0023_050_factor_model_members"
down_revision = "wps_0023_049_factor_weight_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("factor_model_members"):
        return

    op.create_table(
        "factor_model_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("model_run_id", sa.String(length=64), nullable=False),
        sa.Column("factor_code", sa.String(length=64), nullable=False),
        sa.Column("factor_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("coefficient", sa.Float(), nullable=False, server_default="0"),
        sa.Column("normalized_weight", sa.Float(), nullable=False, server_default="0"),
        sa.Column("train_ic", sa.Float(), nullable=True),
        sa.Column("validation_ic", sa.Float(), nullable=True),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("side", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_run_id"], ["factor_model_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_run_id", "factor_code", name="uq_factor_model_member"),
    )
    op.create_index(
        "ix_factor_model_members_model_run_id",
        "factor_model_members",
        ["model_run_id"],
    )
    op.create_index("ix_fmm_factor_code", "factor_model_members", ["factor_code"])
    op.create_index("ix_fmm_side", "factor_model_members", ["side"])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("factor_model_members"):
        return
    for name in ("ix_fmm_side", "ix_fmm_factor_code", "ix_factor_model_members_model_run_id"):
        try:
            op.drop_index(name, table_name="factor_model_members")
        except Exception:
            pass
    op.drop_table("factor_model_members")
