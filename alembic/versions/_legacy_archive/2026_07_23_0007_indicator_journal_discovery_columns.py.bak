"""misc: custom_indicator_versions.change_note + journal_entries.review_tags_json + discovery_tasks.cleanup_count

Revision ID: wps_0023_007_indicator_journal_discovery
Revises: wps_0023_006_trade_setups_cols
Create Date: 2026-07-23 00:07:00.000000

补齐 3 张表各 1 个字段（init_db.py SQLite/MySQL 路径都补了，Alembic 缺失）。
全部 nullable（change_note / cleanup_count 带默认值），向后兼容。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_007_indicator_journal_discovery"
down_revision: Union[str, None] = "wps_0023_006_trade_setups_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


# (table, column, type, default)
_NEW_COLUMNS = [
    ("custom_indicator_versions", "change_note", sa.Text(), ""),
    ("journal_entries", "review_tags_json", sa.Text(), None),
    ("discovery_tasks", "cleanup_count", sa.Integer(), "0"),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table_name, col_name, col_type, default in _NEW_COLUMNS:
        if not _table_exists(inspector, table_name):
            continue
        if not _column_exists(inspector, table_name, col_name):
            if default is not None:
                op.add_column(
                    table_name,
                    sa.Column(col_name, col_type, nullable=True, server_default=default),
                )
            else:
                op.add_column(
                    table_name, sa.Column(col_name, col_type, nullable=True)
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table_name, col_name, _, _ in reversed(_NEW_COLUMNS):
        if not _table_exists(inspector, table_name):
            continue
        if _column_exists(inspector, table_name, col_name):
            op.drop_column(table_name, col_name)
