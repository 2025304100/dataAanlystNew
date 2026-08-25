"""scan_results: warning_days / valid_days / is_frozen / is_active columns

Revision ID: wps_0023_002_scan_results_cols
Revises: wps_0023_001_scan_runs_cache
Create Date: 2026-07-23 00:02:00.000000

补齐 scan_results 表的 4 个字段（init_db.py 仅 SQLite 路径补，MySQL 缺失）。
全部为带默认值的 nullable 列，向后兼容历史数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_002_scan_results_cols"
down_revision: Union[str, None] = "wps_0023_001_scan_runs_cache"
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
    ("warning_days", sa.Integer(), "3"),
    ("valid_days", sa.Integer(), "5"),
    ("is_frozen", sa.Integer(), "0"),
    ("is_active", sa.Integer(), "1"),
]

_NEW_INDEXES = [
    ("ix_scan_results_is_frozen", ["is_frozen"]),
    ("ix_scan_results_is_active", ["is_active"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "scan_results"):
        return

    for col_name, col_type, default in _NEW_COLUMNS:
        if not _column_exists(inspector, "scan_results", col_name):
            op.add_column(
                "scan_results",
                sa.Column(col_name, col_type, nullable=True, server_default=default),
            )

    for idx_name, idx_cols in _NEW_INDEXES:
        if not _index_exists(inspector, "scan_results", idx_name):
            op.create_index(idx_name, "scan_results", idx_cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "scan_results"):
        return

    for idx_name, _ in _NEW_INDEXES:
        if _index_exists(inspector, "scan_results", idx_name):
            op.drop_index(idx_name, table_name="scan_results")

    for col_name, _, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "scan_results", col_name):
            op.drop_column("scan_results", col_name)
