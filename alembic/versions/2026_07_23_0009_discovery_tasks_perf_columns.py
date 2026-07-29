"""discovery_tasks: WP-P.1 performance monitoring columns

Revision ID: wps_0023_009_discovery_tasks_perf
Revises: wps_0023_008_portfolios_auto_trade
Create Date: 2026-07-23 00:09:00.000000

补齐 discovery_tasks 表的 WP-P.1 性能监控字段（init_db.py SQLite/MySQL 路径都补了，Alembic 缺失）。
全部 nullable，向后兼容旧库。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_009_discovery_tasks_perf"
down_revision: Union[str, None] = "wps_0023_008_portfolios_auto_trade"
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
    ("snapshot_id", sa.Integer()),
    ("snapshot_hit", sa.Integer()),
    ("stage_durations_json", sa.Text()),
    ("dirty_symbol_count", sa.Integer()),
    ("reused_score_count", sa.Integer()),
    ("rescored_count", sa.Integer()),
    ("coarse_match_count", sa.Integer()),
    ("advanced_match_count", sa.Integer()),
    ("result_rows_written", sa.Integer()),
    ("cache_key", sa.String(length=128)),
    ("cache_hit", sa.Integer()),
    ("degraded_reason", sa.String(length=64)),
]

_NEW_INDEXES = [
    ("ix_discovery_tasks_snapshot_id", ["snapshot_id"]),
    ("ix_discovery_tasks_cache_key", ["cache_key"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "discovery_tasks"):
        return

    for col_name, col_type in _NEW_COLUMNS:
        if not _column_exists(inspector, "discovery_tasks", col_name):
            op.add_column(
                "discovery_tasks", sa.Column(col_name, col_type, nullable=True)
            )

    for idx_name, idx_cols in _NEW_INDEXES:
        if not _index_exists(inspector, "discovery_tasks", idx_name):
            op.create_index(idx_name, "discovery_tasks", idx_cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "discovery_tasks"):
        return

    for idx_name, _ in _NEW_INDEXES:
        if _index_exists(inspector, "discovery_tasks", idx_name):
            op.drop_index(idx_name, table_name="discovery_tasks")

    for col_name, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "discovery_tasks", col_name):
            op.drop_column("discovery_tasks", col_name)
