"""wps_0023_013_notification_tables.

Revision ID: wps_0023_013_notification_tables
Revises: wps_0023_012_portfolio_members
Create Date: 2026-08-17 00:14:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_013_notification_tables'
down_revision = 'wps_0023_012_portfolio_members'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'notification_channels',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(64), nullable=False, unique=True),
        Column('channel_type', String(16), nullable=False),
        Column('enabled', Integer, nullable=False, server_default='0'),
        Column('status', String(16), nullable=False, server_default='unconfigured'),
        Column('config_encrypted_json', Text, nullable=True),
        Column('config_mask_json', Text, nullable=True),
        Column('verified_at', DateTime, nullable=True),
        Column('last_test_at', DateTime, nullable=True),
        Column('last_test_success', Integer, nullable=True),
        Column('last_error_code', String(64), nullable=True),
        Column('last_error_message', String(500), nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=True),
    )
    op.create_index('ix_notification_channels_name', 'notification_channels', ['name'], unique=True)
    op.create_index('ix_notification_channels_channel_type', 'notification_channels', ['channel_type'])
    op.create_index('ix_notification_channels_status', 'notification_channels', ['status'])
    op.create_index('ix_notification_channels_created_at', 'notification_channels', ['created_at'])

    op.create_table(
        'notification_policies',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(64), nullable=False),
        Column('enabled', Integer, nullable=False, server_default='0'),
        Column('source_types_json', Text, nullable=True),
        Column('min_severity', String(16), nullable=False, server_default='info'),
        Column('scope_type', String(16), nullable=False, server_default='all'),
        Column('scope_ids_json', Text, nullable=True),
        Column('delivery_mode', String(16), nullable=False, server_default='instant'),
        Column('digest_schedule', String(64), nullable=True),
        Column('quiet_hours_json', Text, nullable=True),
        Column('cooldown_minutes', Integer, nullable=False, server_default='0'),
        Column('dedup_window_minutes', Integer, nullable=False, server_default='0'),
        Column('template_id', Integer, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=True),
    )
    op.create_index('ix_notification_policies_name', 'notification_policies', ['name'])
    op.create_index('ix_notification_policies_min_severity', 'notification_policies', ['min_severity'])
    op.create_index('ix_notification_policies_scope_type', 'notification_policies', ['scope_type'])
    op.create_index('ix_notification_policies_template_id', 'notification_policies', ['template_id'])
    op.create_index('ix_notification_policies_created_at', 'notification_policies', ['created_at'])

    op.create_table(
        'notification_policy_channels',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('policy_id', Integer, ForeignKey('notification_policies.id', ondelete='CASCADE'), nullable=False),
        Column('channel_id', Integer, ForeignKey('notification_channels.id', ondelete='CASCADE'), nullable=False),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('policy_id', 'channel_id', name='uq_notification_policy_channels_policy_channel'),
    )
    op.create_index('ix_notification_policy_channels_policy_id', 'notification_policy_channels', ['policy_id'])
    op.create_index('ix_notification_policy_channels_channel_id', 'notification_policy_channels', ['channel_id'])

    op.create_table(
        'notification_outbox',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('event_key', String(128), nullable=False),
        Column('source_type', String(32), nullable=False),
        Column('source_id', Integer, nullable=True),
        Column('event_type', String(64), nullable=False),
        Column('severity', String(16), nullable=False, server_default='info'),
        Column('payload_json', Text, nullable=True),
        Column('channel_id', Integer, ForeignKey('notification_channels.id', ondelete='CASCADE'), nullable=False),
        Column('policy_id', Integer, nullable=True),
        Column('status', String(16), nullable=False, server_default='pending'),
        Column('attempt_count', Integer, nullable=False, server_default='0'),
        Column('max_attempts', Integer, nullable=False, server_default='5'),
        Column('next_retry_at', DateTime, nullable=True),
        Column('last_error_code', String(64), nullable=True),
        Column('last_error_message', String(500), nullable=True),
        Column('read_at', DateTime, nullable=True),
        Column('sent_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=True),
    )
    op.create_index('ix_notification_outbox_event_key', 'notification_outbox', ['event_key'])
    op.create_index('ix_notification_outbox_source_type', 'notification_outbox', ['source_type'])
    op.create_index('ix_notification_outbox_source_id', 'notification_outbox', ['source_id'])
    op.create_index('ix_notification_outbox_event_type', 'notification_outbox', ['event_type'])
    op.create_index('ix_notification_outbox_severity', 'notification_outbox', ['severity'])
    op.create_index('ix_notification_outbox_channel_id', 'notification_outbox', ['channel_id'])
    op.create_index('ix_notification_outbox_policy_id', 'notification_outbox', ['policy_id'])
    op.create_index('ix_notification_outbox_status', 'notification_outbox', ['status'])
    op.create_index('ix_notification_outbox_next_retry_at', 'notification_outbox', ['next_retry_at'])
    op.create_index('ix_notification_outbox_read_at', 'notification_outbox', ['read_at'])
    op.create_index('ix_notification_outbox_created_at', 'notification_outbox', ['created_at'])

    op.create_table(
        'notification_deliveries',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('outbox_id', Integer, ForeignKey('notification_outbox.id', ondelete='CASCADE'), nullable=False),
        Column('channel_id', Integer, nullable=False),
        Column('attempt_number', Integer, nullable=False),
        Column('status', String(16), nullable=False),
        Column('status_code', Integer, nullable=True),
        Column('response_summary', String(500), nullable=True),
        Column('error_code', String(64), nullable=True),
        Column('error_message', String(500), nullable=True),
        Column('duration_ms', Integer, nullable=True),
        Column('sent_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_notification_deliveries_outbox_id', 'notification_deliveries', ['outbox_id'])
    op.create_index('ix_notification_deliveries_channel_id', 'notification_deliveries', ['channel_id'])
    op.create_index('ix_notification_deliveries_status', 'notification_deliveries', ['status'])
    op.create_index('ix_notification_deliveries_created_at', 'notification_deliveries', ['created_at'])

    op.create_table(
        'notification_templates',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(64), nullable=False, unique=True),
        Column('title_template', String(256), nullable=False),
        Column('body_template', Text, nullable=False),
        Column('body_text_template', Text, nullable=True),
        Column('variables_json', Text, nullable=True),
        Column('version', Integer, nullable=False, server_default='1'),
        Column('is_active', Integer, nullable=False, server_default='1'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=True),
    )
    op.create_index('ix_notification_templates_name', 'notification_templates', ['name'], unique=True)
    op.create_index('ix_notification_templates_is_active', 'notification_templates', ['is_active'])
    op.create_index('ix_notification_templates_created_at', 'notification_templates', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_notification_templates_created_at', table_name='notification_templates')
    op.drop_index('ix_notification_templates_is_active', table_name='notification_templates')
    op.drop_index('ix_notification_templates_name', table_name='notification_templates')
    op.drop_table('notification_templates')

    op.drop_index('ix_notification_deliveries_created_at', table_name='notification_deliveries')
    op.drop_index('ix_notification_deliveries_status', table_name='notification_deliveries')
    op.drop_index('ix_notification_deliveries_channel_id', table_name='notification_deliveries')
    op.drop_index('ix_notification_deliveries_outbox_id', table_name='notification_deliveries')
    op.drop_table('notification_deliveries')

    op.drop_index('ix_notification_outbox_created_at', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_read_at', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_next_retry_at', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_status', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_policy_id', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_channel_id', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_severity', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_event_type', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_source_id', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_source_type', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_event_key', table_name='notification_outbox')
    op.drop_table('notification_outbox')

    op.drop_index('ix_notification_policy_channels_channel_id', table_name='notification_policy_channels')
    op.drop_index('ix_notification_policy_channels_policy_id', table_name='notification_policy_channels')
    op.drop_table('notification_policy_channels')

    op.drop_index('ix_notification_policies_created_at', table_name='notification_policies')
    op.drop_index('ix_notification_policies_template_id', table_name='notification_policies')
    op.drop_index('ix_notification_policies_scope_type', table_name='notification_policies')
    op.drop_index('ix_notification_policies_min_severity', table_name='notification_policies')
    op.drop_index('ix_notification_policies_name', table_name='notification_policies')
    op.drop_table('notification_policies')

    op.drop_index('ix_notification_channels_created_at', table_name='notification_channels')
    op.drop_index('ix_notification_channels_status', table_name='notification_channels')
    op.drop_index('ix_notification_channels_channel_type', table_name='notification_channels')
    op.drop_index('ix_notification_channels_name', table_name='notification_channels')
    op.drop_table('notification_channels')
