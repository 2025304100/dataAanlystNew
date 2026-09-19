"""WP-AI.1: ai_sessions / ai_messages / ai_action_audits tables

Revision ID: wps_0023_015_ai_session_tables
Revises: wps_0023_014_sim_orders_attribution
Create Date: 2026-07-23 00:15:00.000000

创建 AI 会话三张表：ai_sessions / ai_messages / ai_action_audits。
init_db.py SQLite/MySQL 路径都补了，Alembic revision 缺失。
幂等：表已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_015_ai_session_tables"
down_revision: Union[str, None] = "wps_0023_014_sim_orders_attribution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── ai_sessions ──
    if not _table_exists(inspector, "ai_sessions"):
        op.create_table(
            "ai_sessions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("title", sa.String(length=256), nullable=False),
            sa.Column("source_page", sa.String(length=64), nullable=True),
            sa.Column("provider", sa.String(length=64), nullable=True),
            sa.Column("model", sa.String(length=128), nullable=True),
            sa.Column("profile_id", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
            sa.Column("context_summary", sa.Text(), nullable=True),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_cost", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ai_sessions_title", "ai_sessions", ["title"], unique=False)
        op.create_index("ix_ai_sessions_source_page", "ai_sessions", ["source_page"], unique=False)
        op.create_index("ix_ai_sessions_profile_id", "ai_sessions", ["profile_id"], unique=False)
        op.create_index("ix_ai_sessions_status", "ai_sessions", ["status"], unique=False)
        op.create_index("ix_ai_sessions_created_at", "ai_sessions", ["created_at"], unique=False)
        op.create_index("idx_ai_sessions_source", "ai_sessions", ["source_page"], unique=False)
        op.create_index("idx_ai_sessions_status", "ai_sessions", ["status"], unique=False)
        op.create_index("idx_ai_sessions_profile", "ai_sessions", ["profile_id"], unique=False)
        op.create_index("idx_ai_sessions_created", "ai_sessions", ["created_at"], unique=False)

    # ── ai_messages ──
    if not _table_exists(inspector, "ai_messages"):
        op.create_table(
            "ai_messages",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("context_summary", sa.Text(), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("model_used", sa.String(length=128), nullable=True),
            sa.Column("provider_used", sa.String(length=64), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["ai_sessions.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ai_messages_session_id", "ai_messages", ["session_id"], unique=False)
        op.create_index("ix_ai_messages_role", "ai_messages", ["role"], unique=False)
        op.create_index("ix_ai_messages_created_at", "ai_messages", ["created_at"], unique=False)
        op.create_index("idx_ai_messages_session", "ai_messages", ["session_id"], unique=False)
        op.create_index("idx_ai_messages_role", "ai_messages", ["role"], unique=False)
        op.create_index("idx_ai_messages_created", "ai_messages", ["created_at"], unique=False)

    # ── ai_action_audits ──
    if not _table_exists(inspector, "ai_action_audits"):
        op.create_table(
            "ai_action_audits",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("message_id", sa.Integer(), nullable=False),
            sa.Column("action_type", sa.String(length=64), nullable=False),
            sa.Column("suggested_payload", sa.Text(), nullable=False),
            sa.Column("preview_result", sa.Text(), nullable=True),
            sa.Column("user_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("final_result", sa.Text(), nullable=True),
            sa.Column("rejected_reason", sa.String(length=256), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["message_id"], ["ai_messages.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ai_action_audits_message_id", "ai_action_audits", ["message_id"], unique=False)
        op.create_index("ix_ai_action_audits_action_type", "ai_action_audits", ["action_type"], unique=False)
        op.create_index("ix_ai_action_audits_user_confirmed", "ai_action_audits", ["user_confirmed"], unique=False)
        op.create_index("ix_ai_action_audits_created_at", "ai_action_audits", ["created_at"], unique=False)
        op.create_index("idx_ai_action_audits_message", "ai_action_audits", ["message_id"], unique=False)
        op.create_index("idx_ai_action_audits_type", "ai_action_audits", ["action_type"], unique=False)
        op.create_index("idx_ai_action_audits_confirmed", "ai_action_audits", ["user_confirmed"], unique=False)
        op.create_index("idx_ai_action_audits_created", "ai_action_audits", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table_name in ["ai_action_audits", "ai_messages", "ai_sessions"]:
        if _table_exists(inspector, table_name):
            op.drop_table(table_name)
