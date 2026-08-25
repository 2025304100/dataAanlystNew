"""wps_0023_020_api_deprecation_logs.

Revision ID: wps_0023_020_api_deprecation_logs
Revises: wps_0023_019_async_tasks_state_machine
Create Date: 2026-08-17 00:21:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_020_api_deprecation_logs'
down_revision = 'wps_0023_019_async_tasks_state_machine'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'api_deprecation_logs',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('endpoint', String(256), nullable=False),
        Column('method', String(8), nullable=False),
        Column('client_ip', String(64), nullable=True),
        Column('user_agent', String(256), nullable=True),
        Column('successor_endpoint', String(256), nullable=True),
        Column('sunset_date', String(32), nullable=True),
        Column('status_code', Integer, nullable=True),
        Column('context_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_api_deprecation_logs_endpoint', 'api_deprecation_logs', ['endpoint'])
    op.create_index('ix_api_deprecation_logs_created_at', 'api_deprecation_logs', ['created_at'])

    op.create_table(
        'data_sync_plans',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('plan_name', String(128), nullable=False, unique=True),
        Column('data_type', String(32), nullable=False),
        Column('source_type', String(32), nullable=False),
        Column('cron_expression', String(64), nullable=False),
        Column('enabled', Integer, nullable=False, server_default='1'),
        Column('config_json', Text, nullable=True),
        Column('last_sync_at', DateTime, nullable=True),
        Column('next_sync_at', DateTime, nullable=True),
        Column('last_sync_status', String(16), nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_data_sync_plans_data_type', 'data_sync_plans', ['data_type'])
    op.create_index('ix_data_sync_plans_source_type', 'data_sync_plans', ['source_type'])
    op.create_index('ix_data_sync_plans_enabled', 'data_sync_plans', ['enabled'])

    op.create_table(
        'data_quality_snapshots',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('snapshot_date', String(32), nullable=False),
        Column('data_type', String(32), nullable=False),
        Column('scope', String(32), nullable=True),
        Column('total_count', Integer, nullable=False, server_default='0'),
        Column('valid_count', Integer, nullable=False, server_default='0'),
        Column('invalid_count', Integer, nullable=False, server_default='0'),
        Column('missing_count', Integer, nullable=False, server_default='0'),
        Column('coverage_pct', Float, nullable=True),
        Column('issues_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_data_quality_snapshots_snapshot_date', 'data_quality_snapshots', ['snapshot_date'])
    op.create_index('ix_data_quality_snapshots_data_type', 'data_quality_snapshots', ['data_type'])


def downgrade() -> None:
    op.drop_index('ix_data_quality_snapshots_data_type', table_name='data_quality_snapshots')
    op.drop_index('ix_data_quality_snapshots_snapshot_date', table_name='data_quality_snapshots')
    op.drop_table('data_quality_snapshots')

    op.drop_index('ix_data_sync_plans_enabled', table_name='data_sync_plans')
    op.drop_index('ix_data_sync_plans_source_type', table_name='data_sync_plans')
    op.drop_index('ix_data_sync_plans_data_type', table_name='data_sync_plans')
    op.drop_table('data_sync_plans')

    op.drop_index('ix_api_deprecation_logs_created_at', table_name='api_deprecation_logs')
    op.drop_index('ix_api_deprecation_logs_endpoint', table_name='api_deprecation_logs')
    op.drop_table('api_deprecation_logs')
