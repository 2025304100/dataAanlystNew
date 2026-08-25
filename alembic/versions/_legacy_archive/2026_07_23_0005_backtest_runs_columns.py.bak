"""backtest_runs: score_weight_mode + WP7.2 snapshot columns

Revision ID: wps_0023_005_backtest_runs_cols
Revises: wps_0023_004_factors_cols
Create Date: 2026-07-23 00:05:00.000000

补齐 backtest_runs 表的回测快照字段（init_db.py SQLite/MySQL 路径都补了，Alembic 缺失）。
全部 nullable，向后兼容历史回测记录。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_005_backtest_runs_cols"
down_revision: Union[str, None] = "wps_0023_004_factors_cols"
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


_NEW_COLUMNS = [
    # score_weight_mode / factor_model_run_id / factor_data_cutoff_at
    ("score_weight_mode", sa.String(length=16), "manual"),
    ("factor_model_run_id", sa.String(length=64), None),
    ("factor_data_cutoff_at", sa.DateTime(), None),
    # WP7.2 回测快照字段
    ("member_snapshot_json", sa.Text(), None),
    ("symbol_ids_json", sa.Text(), None),
    ("excluded_members_json", sa.Text(), None),
    ("portfolio_rule_version_id", sa.Integer(), None),
    ("score_mode", sa.String(length=32), None),
    ("data_cutoff_at", sa.DateTime(), None),
    ("engine_name", sa.String(length=64), None),
    ("engine_version", sa.String(length=32), None),
    ("source_type", sa.String(length=32), None),
]

_NEW_INDEXES = [
    ("ix_backtest_runs_score_weight_mode", ["score_weight_mode"]),
    ("ix_backtest_runs_factor_model_run_id", ["factor_model_run_id"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "backtest_runs"):
        return

    for col_name, col_type, default in _NEW_COLUMNS:
        if not _column_exists(inspector, "backtest_runs", col_name):
            if default is not None:
                op.add_column(
                    "backtest_runs",
                    sa.Column(col_name, col_type, nullable=True, server_default=default),
                )
            else:
                op.add_column(
                    "backtest_runs", sa.Column(col_name, col_type, nullable=True)
                )

    for idx_name, idx_cols in _NEW_INDEXES:
        if not _index_exists(inspector, "backtest_runs", idx_name):
            op.create_index(idx_name, "backtest_runs", idx_cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "backtest_runs"):
        return

    for idx_name, _ in _NEW_INDEXES:
        if _index_exists(inspector, "backtest_runs", idx_name):
            op.drop_index(idx_name, table_name="backtest_runs")

    for col_name, _, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "backtest_runs", col_name):
            op.drop_column("backtest_runs", col_name)
