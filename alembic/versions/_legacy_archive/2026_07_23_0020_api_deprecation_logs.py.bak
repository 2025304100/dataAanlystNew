"""WP9.6: api_deprecation_logs table

Revision ID: wps_0023_020_api_deprecation_logs
Revises: wps_0023_019_async_tasks_state_machine
Create Date: 2026-07-23 00:20:00.000000

创建 api_deprecation_logs 表（WP9.6 API 废弃访问日志）。
init_db.py SQLite/MySQL 路径都补了，Alembic revision 缺失。
幂等：表已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_020_api_deprecation_logs"
down_revision: Union[str, None] = "wps_0023_019_async_tasks_state_machine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "api_deprecation_logs"):
        op.create_table(
            "api_deprecation_logs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("endpoint", sa.String(length=256), nullable=False),
            sa.Column("method", sa.String(length=8), nullable=False),
            sa.Column("client_ip", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=256), nullable=True),
            sa.Column("successor_endpoint", sa.String(length=256), nullable=True),
            sa.Column("sunset_date", sa.String(length=32), nullable=True),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("context_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_api_deprecation_logs_endpoint", "api_deprecation_logs", ["endpoint"], unique=False)
        op.create_index("ix_api_deprecation_logs_created_at", "api_deprecation_logs", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "api_deprecation_logs"):
        op.drop_table("api_deprecation_logs")
