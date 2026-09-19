"""factors: source_type / frequency / default_missing_policy / is_active columns

Revision ID: wps_0023_004_factors_cols
Revises: wps_0023_003_scores_cols
Create Date: 2026-07-23 00:04:00.000000

补齐 factors 表的 4 个字段（init_db.py SQLite/MySQL 路径都补了，Alembic 缺失）。
全部 nullable（is_active 带默认值 1），向后兼容历史因子定义。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_004_factors_cols"
down_revision: Union[str, None] = "wps_0023_003_scores_cols"
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
    ("source_type", sa.String(length=32), None),
    ("frequency", sa.String(length=16), None),
    ("default_missing_policy", sa.String(length=32), "exclude"),
    ("is_active", sa.Integer(), "1"),
]

_NEW_INDEXES = [
    ("ix_factors_is_active", ["is_active"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "factors"):
        return

    for col_name, col_type, default in _NEW_COLUMNS:
        if not _column_exists(inspector, "factors", col_name):
            if default is not None:
                op.add_column(
                    "factors",
                    sa.Column(col_name, col_type, nullable=True, server_default=default),
                )
            else:
                op.add_column("factors", sa.Column(col_name, col_type, nullable=True))

    for idx_name, idx_cols in _NEW_INDEXES:
        if not _index_exists(inspector, "factors", idx_name):
            op.create_index(idx_name, "factors", idx_cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "factors"):
        return

    for idx_name, _ in _NEW_INDEXES:
        if _index_exists(inspector, "factors", idx_name):
            op.drop_index(idx_name, table_name="factors")

    for col_name, _, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "factors", col_name):
            op.drop_column("factors", col_name)
