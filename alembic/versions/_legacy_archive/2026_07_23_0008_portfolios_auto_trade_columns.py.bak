"""portfolios: auto_trade_enabled / auto_trade_last_run_at (P2-3)

Revision ID: wps_0023_008_portfolios_auto_trade
Revises: wps_0023_007_indicator_journal_discovery
Create Date: 2026-07-23 00:08:00.000000

补齐 portfolios 表的自动交易开关字段（init_db.py SQLite/MySQL 路径都补了，Alembic 缺失）。
向后兼容旧库。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_008_portfolios_auto_trade"
down_revision: Union[str, None] = "wps_0023_007_indicator_journal_discovery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


_NEW_COLUMNS = [
    ("auto_trade_enabled", sa.Integer(), "0"),
    ("auto_trade_last_run_at", sa.DateTime(), None),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "portfolios"):
        return

    for col_name, col_type, default in _NEW_COLUMNS:
        if not _column_exists(inspector, "portfolios", col_name):
            if default is not None:
                op.add_column(
                    "portfolios",
                    sa.Column(col_name, col_type, nullable=True, server_default=default),
                )
            else:
                op.add_column(
                    "portfolios", sa.Column(col_name, col_type, nullable=True)
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "portfolios"):
        return

    for col_name, _, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "portfolios", col_name):
            op.drop_column("portfolios", col_name)
