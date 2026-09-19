"""WP6.1: sim_orders attribution columns

Revision ID: wps_0023_014_sim_orders_attribution
Revises: wps_0023_013_notification_tables
Create Date: 2026-07-23 00:14:00.000000

为 sim_orders 表补齐 WP6.1 归因字段（11 列 + 4 索引）：
member_id / source_type / source_id / signal_id / signal_snapshot_json /
rule_version_id / execution_mode / client_order_key / decision_snapshot_json /
rejection_code / rejection_detail。
init_db.py SQLite 路径有补丁，MySQL 路径缺失，Alembic revision 缺失。
幂等：列/索引已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_014_sim_orders_attribution"
down_revision: Union[str, None] = "wps_0023_013_notification_tables"
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


# (column_name, column_type, nullable, server_default)
_NEW_COLUMNS = [
    ("member_id", sa.Integer(), True, None),
    ("source_type", sa.String(length=32), True, None),
    ("source_id", sa.Integer(), True, None),
    ("signal_id", sa.Integer(), True, None),
    ("signal_snapshot_json", sa.Text(), True, None),
    ("rule_version_id", sa.Integer(), True, None),
    ("execution_mode", sa.String(length=16), True, None),
    ("client_order_key", sa.String(length=128), True, None),
    ("decision_snapshot_json", sa.Text(), True, None),
    ("rejection_code", sa.String(length=64), True, None),
    ("rejection_detail", sa.String(length=500), True, None),
]

_NEW_INDEXES = [
    ("idx_sim_orders_member", ["member_id"], False),
    ("idx_sim_orders_source", ["source_type", "source_id"], False),
    ("idx_sim_orders_signal", ["signal_id"], False),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    if not _table_exists(inspector, "sim_orders"):
        return

    for col_name, col_type, nullable, server_default in _NEW_COLUMNS:
        if not _column_exists(inspector, "sim_orders", col_name):
            op.add_column(
                "sim_orders",
                sa.Column(col_name, col_type, nullable=nullable, server_default=server_default),
            )

    for index_name, columns, unique in _NEW_INDEXES:
        if not _index_exists(inspector, "sim_orders", index_name):
            op.create_index(index_name, "sim_orders", columns, unique=unique)

    # client_order_key 唯一索引
    # SQLite: 部分唯一索引（WHERE client_order_key IS NOT NULL）
    # MySQL: 普通唯一索引（NULL 值允许多条，MySQL 唯一索引对 NULL 不去重）
    if not _index_exists(inspector, "sim_orders", "idx_sim_orders_client_key"):
        if dialect == "sqlite":
            op.create_index(
                "idx_sim_orders_client_key",
                "sim_orders",
                ["client_order_key"],
                unique=True,
                sqlite_where=sa.text("client_order_key IS NOT NULL"),
            )
        else:
            op.create_index(
                "idx_sim_orders_client_key",
                "sim_orders",
                ["client_order_key"],
                unique=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "sim_orders"):
        return

    # 待删除的列名集合
    dropping_cols = {col_name for col_name, _, _, _ in _NEW_COLUMNS}

    # 删除所有引用待删列的索引（包括显式命名的 idx_* 和 ORM index=True 自动生成的 ix_*）
    # SQLite 不允许删除仍被索引引用的列
    existing_indexes = inspector.get_indexes("sim_orders")
    for idx in existing_indexes:
        idx_name = idx["name"]
        idx_cols = set(idx.get("column_names", []))
        if idx_cols & dropping_cols:
            op.drop_index(idx_name, table_name="sim_orders")

    # 使用 batch_alter_table 删除列
    # SQLite 的 ALTER TABLE DROP COLUMN 不支持删除被外键引用的列（member_id FK → portfolio_members.id）
    # batch 模式通过重建表来绕过此限制，MySQL 上等价于原生 ALTER TABLE
    cols_to_drop = [
        col_name for col_name, _, _, _ in reversed(_NEW_COLUMNS)
        if _column_exists(inspector, "sim_orders", col_name)
    ]
    if cols_to_drop:
        with op.batch_alter_table("sim_orders") as batch_op:
            for col_name in cols_to_drop:
                batch_op.drop_column(col_name)
