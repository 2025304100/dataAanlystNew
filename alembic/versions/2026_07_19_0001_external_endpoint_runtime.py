"""WP-S.2: external_endpoint_runtime table for circuit breaker state

Revision ID: wps_001_external_endpoint
Revises:
Create Date: 2026-07-19 00:00:00.000000

引入外部接口运行时状态表，承载 WP-S 熔断器状态机：
- interface_key 唯一索引
- state closed/open/half_open
- consecutive_failures / cooldown_until / last_error_*
- request_count / cache_hit_count / fallback_count（可观测性）

注意：本迁移只创建新表，不修改既有业务表，可安全回滚。
旧库升级时由 init_db.py 中的 _ensure_sqlite_external_endpoint_runtime_columns
与 _ensure_mysql_indicator_version_columns 中的对应补丁兜底（幂等）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "wps_001_external_endpoint"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_endpoint_runtime",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("interface_key", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="closed"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interface_key", name="uq_external_endpoint_interface_key"),
    )
    op.create_index(
        "ix_external_endpoint_runtime_interface_key",
        "external_endpoint_runtime",
        ["interface_key"],
        unique=True,
    )
    op.create_index(
        "ix_external_endpoint_runtime_host",
        "external_endpoint_runtime",
        ["host"],
        unique=False,
    )
    op.create_index(
        "ix_external_endpoint_runtime_state",
        "external_endpoint_runtime",
        ["state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_endpoint_runtime_state", table_name="external_endpoint_runtime"
    )
    op.drop_index(
        "ix_external_endpoint_runtime_host", table_name="external_endpoint_runtime"
    )
    op.drop_index(
        "ix_external_endpoint_runtime_interface_key",
        table_name="external_endpoint_runtime",
    )
    op.drop_table("external_endpoint_runtime")
