"""WP4.1: portfolio_members table

Revision ID: wps_0023_012_portfolio_members
Revises: wps_0023_011_opportunity_transitions
Create Date: 2026-07-23 00:12:00.000000

创建 portfolio_members 表（组合成员）。
init_db.py SQLite/MySQL 路径都补了，Alembic 缺失。
部分唯一索引 idx_portfolio_members_active（effective_to IS NULL）：
- SQLite：通过 sqlite_where 实现 partial unique index
- MySQL：不支持 partial index，跳过（由应用层检查 effective_to IS NULL 唯一性）
幂等：表已存在时仅补缺失索引。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_012_portfolio_members"
down_revision: Union[str, None] = "wps_0023_011_opportunity_transitions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return index_name in {i["name"] for i in inspector.get_indexes(table_name)}


_TABLE = "portfolio_members"

_REGULAR_INDEXES = [
    ("idx_portfolio_members_status", ["status"]),
    ("idx_portfolio_members_source", ["source_type", "source_id"]),
    ("idx_portfolio_members_execution", ["execution_mode"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    if _table_exists(inspector, _TABLE):
        # 表已存在，仅补缺失的索引（幂等）
        for idx_name, idx_cols in _REGULAR_INDEXES:
            if not _index_exists(inspector, _TABLE, idx_name):
                op.create_index(idx_name, _TABLE, idx_cols, unique=False)
        # SQLite 部分唯一索引
        if dialect == "sqlite" and not _index_exists(
            inspector, _TABLE, "idx_portfolio_members_active"
        ):
            op.create_index(
                "idx_portfolio_members_active",
                _TABLE,
                ["portfolio_id", "symbol_id"],
                unique=True,
                sqlite_where=sa.text("effective_to IS NULL"),
            )
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("execution_mode", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("entry_rule_version_id", sa.Integer(), nullable=True),
        sa.Column("exit_rule_version_id", sa.Integer(), nullable=True),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("manual_lock", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_portfolio_members_portfolio_id", _TABLE, ["portfolio_id"], unique=False)
    op.create_index("ix_portfolio_members_symbol_id", _TABLE, ["symbol_id"], unique=False)
    op.create_index("ix_portfolio_members_source_id", _TABLE, ["source_id"], unique=False)
    op.create_index("ix_portfolio_members_created_at", _TABLE, ["created_at"], unique=False)

    for idx_name, idx_cols in _REGULAR_INDEXES:
        op.create_index(idx_name, _TABLE, idx_cols, unique=False)

    # SQLite 部分唯一索引（MySQL 不支持 partial index，跳过）
    if dialect == "sqlite":
        op.create_index(
            "idx_portfolio_members_active",
            _TABLE,
            ["portfolio_id", "symbol_id"],
            unique=True,
            sqlite_where=sa.text("effective_to IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, _TABLE):
        return

    dialect = bind.dialect.name
    if dialect == "sqlite" and _index_exists(inspector, _TABLE, "idx_portfolio_members_active"):
        op.drop_index("idx_portfolio_members_active", table_name=_TABLE)

    for idx_name, _ in _REGULAR_INDEXES:
        if _index_exists(inspector, _TABLE, idx_name):
            op.drop_index(idx_name, table_name=_TABLE)

    for idx_name in [
        "ix_portfolio_members_created_at",
        "ix_portfolio_members_source_id",
        "ix_portfolio_members_symbol_id",
        "ix_portfolio_members_portfolio_id",
    ]:
        if _index_exists(inspector, _TABLE, idx_name):
            op.drop_index(idx_name, table_name=_TABLE)

    op.drop_table(_TABLE)
