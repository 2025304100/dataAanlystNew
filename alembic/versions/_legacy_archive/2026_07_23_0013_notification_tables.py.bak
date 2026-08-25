"""WP-MSG.1: notification_* tables (6 tables)

Revision ID: wps_0023_013_notification_tables
Revises: wps_0023_012_portfolio_members
Create Date: 2026-07-23 00:13:00.000000

创建 6 张通知表：notification_channels / notification_policies /
notification_policy_channels / notification_outbox / notification_deliveries /
notification_templates。
init_db.py SQLite/MySQL 路径都补了，Alembic 缺失。
部分唯一索引 idx_no_event_channel_pending（status != 'sent'）：
- SQLite：通过 sqlite_where 实现 partial unique index
- MySQL：不支持 partial index，跳过（由应用层检查 status != 'sent' 唯一性）
幂等：表已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_013_notification_tables"
down_revision: Union[str, None] = "wps_0023_012_portfolio_members"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return index_name in {i["name"] for i in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    # ── notification_channels ──
    if not _table_exists(inspector, "notification_channels"):
        op.create_table(
            "notification_channels",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("channel_type", sa.String(length=16), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="unconfigured"),
            sa.Column("config_encrypted_json", sa.Text(), nullable=True),
            sa.Column("config_mask_json", sa.Text(), nullable=True),
            sa.Column("verified_at", sa.DateTime(), nullable=True),
            sa.Column("last_test_at", sa.DateTime(), nullable=True),
            sa.Column("last_test_success", sa.Boolean(), nullable=True),
            sa.Column("last_error_code", sa.String(length=64), nullable=True),
            sa.Column("last_error_message", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_notification_channels_name"),
        )
        op.create_index("ix_notification_channels_name", "notification_channels", ["name"], unique=True)
        op.create_index("ix_notification_channels_channel_type", "notification_channels", ["channel_type"], unique=False)
        op.create_index("ix_notification_channels_status", "notification_channels", ["status"], unique=False)
        op.create_index("ix_notification_channels_created_at", "notification_channels", ["created_at"], unique=False)

    # ── notification_policies ──
    if not _table_exists(inspector, "notification_policies"):
        op.create_table(
            "notification_policies",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("source_types_json", sa.Text(), nullable=True),
            sa.Column("min_severity", sa.String(length=16), nullable=False, server_default="info"),
            sa.Column("scope_type", sa.String(length=16), nullable=False, server_default="all"),
            sa.Column("scope_ids_json", sa.Text(), nullable=True),
            sa.Column("delivery_mode", sa.String(length=16), nullable=False, server_default="instant"),
            sa.Column("digest_schedule", sa.String(length=64), nullable=True),
            sa.Column("quiet_hours_json", sa.Text(), nullable=True),
            sa.Column("cooldown_minutes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("dedup_window_minutes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("template_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notification_policies_name", "notification_policies", ["name"], unique=False)
        op.create_index("ix_notification_policies_min_severity", "notification_policies", ["min_severity"], unique=False)
        op.create_index("ix_notification_policies_scope_type", "notification_policies", ["scope_type"], unique=False)
        op.create_index("ix_notification_policies_template_id", "notification_policies", ["template_id"], unique=False)
        op.create_index("ix_notification_policies_created_at", "notification_policies", ["created_at"], unique=False)

    # ── notification_policy_channels ──
    if not _table_exists(inspector, "notification_policy_channels"):
        op.create_table(
            "notification_policy_channels",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("policy_id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["policy_id"], ["notification_policies.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("policy_id", "channel_id", name="uq_notification_policy_channels_policy_channel"),
        )
        op.create_index("ix_notification_policy_channels_policy_id", "notification_policy_channels", ["policy_id"], unique=False)
        op.create_index("ix_notification_policy_channels_channel_id", "notification_policy_channels", ["channel_id"], unique=False)

    # ── notification_outbox ──
    if not _table_exists(inspector, "notification_outbox"):
        op.create_table(
            "notification_outbox",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_key", sa.String(length=128), nullable=False),
            sa.Column("source_type", sa.String(length=32), nullable=False),
            sa.Column("source_id", sa.Integer(), nullable=True),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False, server_default="info"),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("policy_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("next_retry_at", sa.DateTime(), nullable=True),
            sa.Column("last_error_code", sa.String(length=64), nullable=True),
            sa.Column("last_error_message", sa.String(length=500), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notification_outbox_event_key", "notification_outbox", ["event_key"], unique=False)
        op.create_index("ix_notification_outbox_source_type", "notification_outbox", ["source_type"], unique=False)
        op.create_index("ix_notification_outbox_source_id", "notification_outbox", ["source_id"], unique=False)
        op.create_index("ix_notification_outbox_event_type", "notification_outbox", ["event_type"], unique=False)
        op.create_index("ix_notification_outbox_severity", "notification_outbox", ["severity"], unique=False)
        op.create_index("ix_notification_outbox_channel_id", "notification_outbox", ["channel_id"], unique=False)
        op.create_index("ix_notification_outbox_policy_id", "notification_outbox", ["policy_id"], unique=False)
        op.create_index("ix_notification_outbox_status", "notification_outbox", ["status"], unique=False)
        op.create_index("ix_notification_outbox_next_retry_at", "notification_outbox", ["next_retry_at"], unique=False)
        op.create_index("ix_notification_outbox_created_at", "notification_outbox", ["created_at"], unique=False)
        op.create_index("idx_no_status", "notification_outbox", ["status"], unique=False)
        op.create_index("idx_no_source", "notification_outbox", ["source_type", "source_id"], unique=False)
        op.create_index("idx_no_retry", "notification_outbox", ["next_retry_at"], unique=False)

    # 部分唯一索引 idx_no_event_channel_pending（SQLite sqlite_where；MySQL 跳过）
    if dialect == "sqlite" and _table_exists(inspector, "notification_outbox") \
            and not _index_exists(inspector, "notification_outbox", "idx_no_event_channel_pending"):
        op.create_index(
            "idx_no_event_channel_pending",
            "notification_outbox",
            ["event_key", "channel_id"],
            unique=True,
            sqlite_where=sa.text("status != 'sent'"),
        )

    # ── notification_deliveries ──
    if not _table_exists(inspector, "notification_deliveries"):
        op.create_table(
            "notification_deliveries",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("outbox_id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("response_summary", sa.String(length=500), nullable=True),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.String(length=500), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["outbox_id"], ["notification_outbox.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notification_deliveries_outbox_id", "notification_deliveries", ["outbox_id"], unique=False)
        op.create_index("ix_notification_deliveries_channel_id", "notification_deliveries", ["channel_id"], unique=False)
        op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"], unique=False)
        op.create_index("ix_notification_deliveries_created_at", "notification_deliveries", ["created_at"], unique=False)

    # ── notification_templates ──
    if not _table_exists(inspector, "notification_templates"):
        op.create_table(
            "notification_templates",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("title_template", sa.String(length=256), nullable=False),
            sa.Column("body_template", sa.Text(), nullable=False),
            sa.Column("body_text_template", sa.Text(), nullable=True),
            sa.Column("variables_json", sa.Text(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_notification_templates_name"),
        )
        op.create_index("ix_notification_templates_name", "notification_templates", ["name"], unique=True)
        op.create_index("ix_notification_templates_created_at", "notification_templates", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    # 按依赖反向顺序删除
    if dialect == "sqlite" and _index_exists(inspector, "notification_outbox", "idx_no_event_channel_pending"):
        op.drop_index("idx_no_event_channel_pending", table_name="notification_outbox")

    for table_name in [
        "notification_templates",
        "notification_deliveries",
        "notification_outbox",
        "notification_policy_channels",
        "notification_policies",
        "notification_channels",
    ]:
        if _table_exists(inspector, table_name):
            op.drop_table(table_name)
