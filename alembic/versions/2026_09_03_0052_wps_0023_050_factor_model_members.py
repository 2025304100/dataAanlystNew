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
        _copy_legacy_weights(bind)
        return
    if inspector.has_table("factor_model_members_archive"):
        op.rename_table("factor_model_members_archive", "factor_model_members")
        _copy_legacy_weights(bind)
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
    _copy_legacy_weights(bind)


def _copy_legacy_weights(bind) -> None:
    """Copy only unambiguous authentic rows; never synthesize missing artifacts."""
    if not sa.inspect(bind).has_table("factor_weight_snapshots_legacy"):
        return
    metadata = sa.MetaData()
    legacy = sa.Table("factor_weight_snapshots_legacy", metadata, autoload_with=bind)
    members = sa.Table("factor_model_members", metadata, autoload_with=bind)
    unique_pairs = sa.select(legacy.c.model_run_id, legacy.c.factor_code).group_by(
        legacy.c.model_run_id, legacy.c.factor_code,
    ).having(sa.func.count() == 1).subquery()
    source = legacy.join(unique_pairs, sa.and_(
        legacy.c.model_run_id == unique_pairs.c.model_run_id,
        legacy.c.factor_code == unique_pairs.c.factor_code,
    ))
    columns = ("model_run_id", "factor_code", "factor_version", "coefficient", "normalized_weight", "train_ic", "validation_ic")
    existing = sa.select(members.c.id).where(
        members.c.model_run_id == legacy.c.model_run_id, members.c.factor_code == legacy.c.factor_code,
    ).exists()
    bind.execute(members.insert().from_select(
        [*columns, "created_at"],
        sa.select(*(legacy.c[c] for c in columns), sa.func.coalesce(legacy.c.created_at, sa.func.current_timestamp()))
        .select_from(source).where(~existing),
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("factor_model_members"):
        return
    if sa.inspect(bind).has_table("factor_model_members_archive"):
        raise RuntimeError("member weight archive already exists; refusing to overwrite it")
    op.rename_table("factor_model_members", "factor_model_members_archive")
