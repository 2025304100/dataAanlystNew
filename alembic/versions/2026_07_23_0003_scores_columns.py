"""scores: P0 scoring config snapshot + dynamic factor model columns

Revision ID: wps_0023_003_scores_cols
Revises: wps_0023_002_scan_results_cols
Create Date: 2026-07-23 00:03:00.000000

补齐 scores 表的评分配置快照与动态因子模型字段（P0 + WP-P.2）。
init_db.py 的 SQLite/MySQL 路径都补了这些字段，但 Alembic 缺失 revision。
全部 nullable，向后兼容历史评分数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_003_scores_cols"
down_revision: Union[str, None] = "wps_0023_002_scan_results_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return index_name in {i["name"] for i in inspector.get_indexes(table_name)}


# (column_name, type, server_default)
_NEW_COLUMNS = [
    # 时点评分分项
    ("breakout_score", sa.Float(), None),
    ("pullback_score", sa.Float(), None),
    ("overheat_penalty", sa.Float(), None),
    # P0 评分配置快照
    ("scoring_asset_type", sa.String(length=16), None),
    ("scoring_config_id", sa.Integer(), None),
    ("scoring_preset_key", sa.String(length=64), None),
    ("scoring_preset_name", sa.String(length=128), None),
    ("scoring_config_version", sa.Integer(), None),
    ("scoring_config_snapshot_json", sa.Text(), None),
    ("dimension_scores_json", sa.Text(), None),
    ("factor_scores_json", sa.Text(), None),
    # 动态因子模型字段
    ("weight_mode", sa.String(length=16), "manual"),
    ("factor_model_run_id", sa.String(length=64), None),
    ("factor_data_cutoff_at", sa.DateTime(), None),
    ("factor_quality_score", sa.Float(), None),
    ("factor_timing_score", sa.Float(), None),
    ("model_alpha_score", sa.Float(), None),
    ("macro_regime", sa.String(length=24), None),
    ("macro_position_multiplier", sa.Float(), None),
]

_NEW_INDEXES = [
    ("ix_scores_scoring_asset_type", ["scoring_asset_type"]),
    ("ix_scores_scoring_config_id", ["scoring_config_id"]),
    ("ix_scores_weight_mode", ["weight_mode"]),
    ("ix_scores_factor_model_run_id", ["factor_model_run_id"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "scores"):
        return

    for col_name, col_type, default in _NEW_COLUMNS:
        if not _column_exists(inspector, "scores", col_name):
            if default is not None:
                op.add_column(
                    "scores",
                    sa.Column(col_name, col_type, nullable=True, server_default=default),
                )
            else:
                op.add_column(
                    "scores", sa.Column(col_name, col_type, nullable=True)
                )

    for idx_name, idx_cols in _NEW_INDEXES:
        if not _index_exists(inspector, "scores", idx_name):
            op.create_index(idx_name, "scores", idx_cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "scores"):
        return

    for idx_name, _ in _NEW_INDEXES:
        if _index_exists(inspector, "scores", idx_name):
            op.drop_index(idx_name, table_name="scores")

    for col_name, _, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "scores", col_name):
            op.drop_column("scores", col_name)
