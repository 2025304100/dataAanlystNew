"""watchlist_items: WP-P.7 score_snapshot_json + WP2.1 extend columns

Revision ID: wps_0023_010_watchlist_items_extend
Revises: wps_0023_009_discovery_tasks_perf
Create Date: 2026-07-23 00:10:00.000000

补齐 watchlist_items 表的 WP-P.7 score_snapshot_json + WP2.1 扩展字段
（init_db.py SQLite/MySQL 路径都补了，Alembic 缺失）。
WP-P.7 的 score_snapshot_json 仅 SQLite 路径补，MySQL 缺失——本 revision 补齐。
带默认值的 NOT NULL 列先以 nullable=True + server_default 添加（符合硬约束）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_010_watchlist_items_extend"
down_revision: Union[str, None] = "wps_0023_009_discovery_tasks_perf"
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


# (column_name, type, server_default) — 先 nullable=True + default 添加
# 模型中 NOT NULL 的列在回填后再加约束（本 revision 不加 NOT NULL）
_NEW_COLUMNS = [
    # WP-P.7
    ("score_snapshot_json", sa.Text(), None),
    # WP2.1 扩展字段
    ("origin_type", sa.String(length=32), "manual"),
    ("origin_id", sa.Integer(), None),
    ("reason_json", sa.Text(), None),
    ("status", sa.String(length=16), "watching"),
    ("priority", sa.Integer(), "0"),
    ("tags_json", sa.Text(), None),
    ("target_portfolio_id", sa.Integer(), None),
    ("updated_at", sa.DateTime(), None),
    ("archived_at", sa.DateTime(), None),
]

_NEW_INDEXES = [
    ("idx_watchlist_items_origin_id", ["origin_id"]),
    ("idx_watchlist_items_status", ["status"]),
    ("idx_watchlist_items_target_portfolio_id", ["target_portfolio_id"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "watchlist_items"):
        return

    for col_name, col_type, default in _NEW_COLUMNS:
        if not _column_exists(inspector, "watchlist_items", col_name):
            if default is not None:
                op.add_column(
                    "watchlist_items",
                    sa.Column(col_name, col_type, nullable=True, server_default=default),
                )
            else:
                op.add_column(
                    "watchlist_items",
                    sa.Column(col_name, col_type, nullable=True),
                )

    for idx_name, idx_cols in _NEW_INDEXES:
        if not _index_exists(inspector, "watchlist_items", idx_name):
            op.create_index(idx_name, "watchlist_items", idx_cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "watchlist_items"):
        return

    # 删除引用待删列的索引（包括 ORM index=True 自动生成的 ix_*）
    dropping_cols = {col_name for col_name, _, _ in _NEW_COLUMNS}
    existing_indexes = inspector.get_indexes("watchlist_items")
    for idx in existing_indexes:
        idx_name = idx["name"]
        idx_cols = set(idx.get("column_names", []))
        if idx_cols & dropping_cols:
            op.drop_index(idx_name, table_name="watchlist_items")

    # 使用 batch_alter_table 删除列
    # SQLite 的 ALTER TABLE DROP COLUMN 不支持删除被外键引用的列
    # （target_portfolio_id FK → portfolios.id）
    cols_to_drop = [
        col_name for col_name, _, _ in reversed(_NEW_COLUMNS)
        if _column_exists(inspector, "watchlist_items", col_name)
    ]
    if cols_to_drop:
        with op.batch_alter_table("watchlist_items") as batch_op:
            for col_name in cols_to_drop:
                batch_op.drop_column(col_name)
