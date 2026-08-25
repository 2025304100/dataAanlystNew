"""wps_0023_015_ai_session_tables.

Revision ID: wps_0023_015_ai_session_tables
Revises: wps_0023_014_sim_orders_attribution
Create Date: 2026-08-17 00:16:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_015_ai_session_tables'
down_revision = 'wps_0023_014_sim_orders_attribution'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_sessions',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('title', String(256), nullable=False),
        Column('source_page', String(64), nullable=True),
        Column('provider', String(64), nullable=True),
        Column('model', String(128), nullable=True),
        Column('profile_id', String(64), nullable=True),
        Column('status', String(16), nullable=False, server_default='active'),
        Column('context_summary', Text, nullable=True),
        Column('total_tokens', Integer, nullable=False, server_default='0'),
        Column('total_cost', Float, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=True),
        Column('deleted_at', DateTime, nullable=True),
    )
    op.create_index('ix_ai_sessions_title', 'ai_sessions', ['title'])
    op.create_index('ix_ai_sessions_source_page', 'ai_sessions', ['source_page'])
    op.create_index('ix_ai_sessions_profile_id', 'ai_sessions', ['profile_id'])
    op.create_index('ix_ai_sessions_status', 'ai_sessions', ['status'])
    op.create_index('ix_ai_sessions_created_at', 'ai_sessions', ['created_at'])

    op.create_table(
        'ai_messages',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('session_id', Integer, ForeignKey('ai_sessions.id', ondelete='CASCADE'), nullable=False),
        Column('role', String(16), nullable=False),
        Column('content', Text, nullable=False),
        Column('context_summary', Text, nullable=True),
        Column('prompt_tokens', Integer, nullable=False, server_default='0'),
        Column('completion_tokens', Integer, nullable=False, server_default='0'),
        Column('total_tokens', Integer, nullable=False, server_default='0'),
        Column('latency_ms', Integer, nullable=True),
        Column('model_used', String(128), nullable=True),
        Column('provider_used', String(64), nullable=True),
        Column('metadata_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_ai_messages_session_id', 'ai_messages', ['session_id'])
    op.create_index('ix_ai_messages_role', 'ai_messages', ['role'])
    op.create_index('ix_ai_messages_created_at', 'ai_messages', ['created_at'])

    op.create_table(
        'ai_action_audits',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('message_id', Integer, ForeignKey('ai_messages.id', ondelete='CASCADE'), nullable=False),
        Column('action_type', String(64), nullable=False),
        Column('suggested_payload', Text, nullable=False),
        Column('preview_result', Text, nullable=True),
        Column('user_confirmed', Integer, nullable=False, server_default='0'),
        Column('confirmed_at', DateTime, nullable=True),
        Column('final_result', Text, nullable=True),
        Column('rejected_reason', String(256), nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_ai_action_audits_message_id', 'ai_action_audits', ['message_id'])
    op.create_index('ix_ai_action_audits_action_type', 'ai_action_audits', ['action_type'])
    op.create_index('ix_ai_action_audits_user_confirmed', 'ai_action_audits', ['user_confirmed'])
    op.create_index('ix_ai_action_audits_created_at', 'ai_action_audits', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_ai_action_audits_created_at', table_name='ai_action_audits')
    op.drop_index('ix_ai_action_audits_user_confirmed', table_name='ai_action_audits')
    op.drop_index('ix_ai_action_audits_action_type', table_name='ai_action_audits')
    op.drop_index('ix_ai_action_audits_message_id', table_name='ai_action_audits')
    op.drop_table('ai_action_audits')

    op.drop_index('ix_ai_messages_created_at', table_name='ai_messages')
    op.drop_index('ix_ai_messages_role', table_name='ai_messages')
    op.drop_index('ix_ai_messages_session_id', table_name='ai_messages')
    op.drop_table('ai_messages')

    op.drop_index('ix_ai_sessions_created_at', table_name='ai_sessions')
    op.drop_index('ix_ai_sessions_status', table_name='ai_sessions')
    op.drop_index('ix_ai_sessions_profile_id', table_name='ai_sessions')
    op.drop_index('ix_ai_sessions_source_page', table_name='ai_sessions')
    op.drop_index('ix_ai_sessions_title', table_name='ai_sessions')
    op.drop_table('ai_sessions')
