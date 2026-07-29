"""trade_setups: manual_overrides_json / field_sources_json / manual_tranche_plan_json

Revision ID: wps_0023_006_trade_setups_cols
Revises: wps_0023_005_backtest_runs_cols
Create Date: 2026-07-23 00:06:00.000000

补齐 trade_setups 表的 3 个 JSON 字段（init_db.py 仅 SQLite 路径补，MySQL 缺失）。
全部 nullable，向后兼容历史交易设置。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_006_trade_setups_cols"
down_revision: Union[str, None] = "wps_0023_005_backtest_runs_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


_NEW_COLUMNS = [
    ("manual_overrides_json", sa.Text()),
    ("field_sources_json", sa.Text()),
    ("manual_tranche_plan_json", sa.Text()),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "trade_setups"):
        return

    for col_name, col_type in _NEW_COLUMNS:
        if not _column_exists(inspector, "trade_setups", col_name):
            op.add_column(
                "trade_setups", sa.Column(col_name, col_type, nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "trade_setups"):
        return

    for col_name, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "trade_setups", col_name):
            op.drop_column("trade_setups", col_name)
