"""WP-P.2: discovery_score_snapshots / discovery_score_snapshot_items tables

Revision ID: wps_0023_018_discovery_score_snapshots
Revises: wps_0023_017_ai_profiles
Create Date: 2026-07-23 00:18:00.000000

创建评分快照两张表：discovery_score_snapshots（主表）+ discovery_score_snapshot_items（明细表）。
init_db.py SQLite/MySQL 路径都补了，Alembic revision 缺失。
幂等：表已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_018_discovery_score_snapshots"
down_revision: Union[str, None] = "wps_0023_017_ai_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── discovery_score_snapshots（主表）──
    if not _table_exists(inspector, "discovery_score_snapshots"):
        op.create_table(
            "discovery_score_snapshots",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("scope", sa.String(length=32), nullable=False),
            sa.Column("trade_date", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="building"),
            sa.Column("scoring_config_id", sa.Integer(), nullable=True),
            sa.Column("scoring_config_version", sa.Integer(), nullable=True),
            sa.Column("weight_mode", sa.String(length=16), nullable=True),
            sa.Column("factor_model_run_id", sa.String(length=64), nullable=True),
            sa.Column("data_cutoff_at", sa.DateTime(), nullable=True),
            sa.Column("generated_at", sa.DateTime(), nullable=True),
            sa.Column("build_duration_seconds", sa.Float(), nullable=True),
            sa.Column("source_task_id", sa.String(length=64), nullable=True),
            sa.Column("symbol_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("coverage_pct", sa.Float(), nullable=True, server_default="0.0"),
            sa.Column("dirty_symbol_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("error_summary_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("scope", "status", "trade_date", name="uq_snapshot_scope_status_date"),
        )
        op.create_index("ix_discovery_score_snapshots_scope", "discovery_score_snapshots", ["scope"], unique=False)
        op.create_index("ix_discovery_score_snapshots_trade_date", "discovery_score_snapshots", ["trade_date"], unique=False)
        op.create_index("ix_discovery_score_snapshots_status", "discovery_score_snapshots", ["status"], unique=False)
        op.create_index("ix_discovery_score_snapshots_scoring_config_id", "discovery_score_snapshots", ["scoring_config_id"], unique=False)
        op.create_index("ix_discovery_score_snapshots_source_task_id", "discovery_score_snapshots", ["source_task_id"], unique=False)
        op.create_index(
            "ix_snapshot_scope_status_date",
            "discovery_score_snapshots",
            ["scope", "status", "trade_date", "generated_at"],
            unique=False,
        )

    # ── discovery_score_snapshot_items（明细表）──
    if not _table_exists(inspector, "discovery_score_snapshot_items"):
        op.create_table(
            "discovery_score_snapshot_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("snapshot_id", sa.Integer(), nullable=False),
            sa.Column("universe_symbol_id", sa.Integer(), nullable=True),
            sa.Column("symbol_id", sa.Integer(), nullable=True),
            sa.Column("quality_score", sa.Float(), nullable=True),
            sa.Column("timing_score", sa.Float(), nullable=True),
            sa.Column("priority_score", sa.Float(), nullable=True),
            sa.Column("dimension_scores_json", sa.Text(), nullable=True),
            sa.Column("stage", sa.String(length=32), nullable=True),
            sa.Column("action", sa.String(length=32), nullable=True),
            sa.Column("data_credibility", sa.Float(), nullable=True),
            sa.Column("health_summary_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["snapshot_id"], ["discovery_score_snapshots.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("snapshot_id", "universe_symbol_id", name="uq_snapshot_item_symbol"),
        )
        op.create_index("ix_discovery_score_snapshot_items_snapshot_id", "discovery_score_snapshot_items", ["snapshot_id"], unique=False)
        op.create_index("ix_discovery_score_snapshot_items_universe_symbol_id", "discovery_score_snapshot_items", ["universe_symbol_id"], unique=False)
        op.create_index("ix_discovery_score_snapshot_items_symbol_id", "discovery_score_snapshot_items", ["symbol_id"], unique=False)
        op.create_index(
            "ix_snapshot_item_priority",
            "discovery_score_snapshot_items",
            ["snapshot_id", "priority_score"],
            unique=False,
        )
        op.create_index(
            "ix_snapshot_item_action_stage",
            "discovery_score_snapshot_items",
            ["snapshot_id", "action", "stage", "priority_score"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "discovery_score_snapshot_items"):
        op.drop_table("discovery_score_snapshot_items")

    if _table_exists(inspector, "discovery_score_snapshots"):
        op.drop_table("discovery_score_snapshots")
