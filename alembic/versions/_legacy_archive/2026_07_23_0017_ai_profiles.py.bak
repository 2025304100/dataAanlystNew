"""WP-AI.2: ai_profiles table

Revision ID: wps_0023_017_ai_profiles
Revises: wps_0023_016_portfolio_reviews
Create Date: 2026-07-23 00:17:00.000000

创建 ai_profiles 表（WP-AI.2 多 Profile 主备降级）。
init_db.py SQLite/MySQL 路径都补了，Alembic revision 缺失。
幂等：表已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_017_ai_profiles"
down_revision: Union[str, None] = "wps_0023_016_portfolio_reviews"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "ai_profiles"):
        op.create_table(
            "ai_profiles",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("base_url", sa.String(length=256), nullable=True),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("auth_type", sa.String(length=32), nullable=True),
            sa.Column("secret_key_ref", sa.String(length=128), nullable=True),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="4096"),
            sa.Column("max_context_tokens", sa.Integer(), nullable=False, server_default="8192"),
            sa.Column("daily_request_limit", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("max_concurrent", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("purpose", sa.String(length=64), nullable=False, server_default="all"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("health_status", sa.String(length=16), nullable=False, server_default="unknown"),
            sa.Column("last_health_check", sa.DateTime(), nullable=True),
            sa.Column("daily_request_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("daily_request_reset_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_ai_profiles_name"),
        )
        op.create_index("ix_ai_profiles_name", "ai_profiles", ["name"], unique=True)
        op.create_index("ix_ai_profiles_provider", "ai_profiles", ["provider"], unique=False)
        op.create_index("ix_ai_profiles_purpose", "ai_profiles", ["purpose"], unique=False)
        op.create_index("ix_ai_profiles_priority", "ai_profiles", ["priority"], unique=False)
        op.create_index("ix_ai_profiles_is_enabled", "ai_profiles", ["is_enabled"], unique=False)
        op.create_index("ix_ai_profiles_health_status", "ai_profiles", ["health_status"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "ai_profiles"):
        op.drop_table("ai_profiles")
