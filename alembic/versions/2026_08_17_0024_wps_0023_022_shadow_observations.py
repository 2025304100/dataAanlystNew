"""wps_0023_022_shadow_observations.

Revision ID: wps_0023_022_shadow_observations
Revises: wps_0023_021_factor_library_lifecycle
Create Date: 2026-08-17 00:24:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_022_shadow_observations'
down_revision = 'wps_0023_021_factor_library_lifecycle'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'discovery_plans',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('plan_name', String(128), nullable=False, unique=True),
        Column('scope', String(32), nullable=False),
        Column('strategy_type', String(32), nullable=False),
        Column('config_json', Text, nullable=True),
        Column('schedule_cron', String(64), nullable=True),
        Column('enabled', Integer, nullable=False, server_default='1'),
        Column('last_run_id', String(64), nullable=True),
        Column('last_run_at', DateTime, nullable=True),
        Column('next_run_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_discovery_plans_scope', 'discovery_plans', ['scope'])
    op.create_index('ix_discovery_plans_strategy_type', 'discovery_plans', ['strategy_type'])
    op.create_index('ix_discovery_plans_enabled', 'discovery_plans', ['enabled'])

    op.create_table(
        'data_sync_plan_executions',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('plan_id', Integer, nullable=False),
        Column('execution_type', String(32), nullable=False),
        Column('status', String(16), nullable=False, server_default='pending'),
        Column('started_at', DateTime, nullable=True),
        Column('finished_at', DateTime, nullable=True),
        Column('records_processed', Integer, nullable=False, server_default='0'),
        Column('records_failed', Integer, nullable=False, server_default='0'),
        Column('error_message', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_data_sync_plan_executions_plan_id', 'data_sync_plan_executions', ['plan_id'])
    op.create_index('ix_data_sync_plan_executions_status', 'data_sync_plan_executions', ['status'])
    op.create_index('ix_data_sync_plan_executions_created_at', 'data_sync_plan_executions', ['created_at'])

    op.create_table(
        'opportunity_observation_notes',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('opportunity_type', String(32), nullable=False),
        Column('opportunity_id', Integer, nullable=False),
        Column('note_type', String(32), nullable=False),
        Column('content', Text, nullable=False),
        Column('created_by', String(128), nullable=False, server_default='local_user'),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_opportunity_observation_notes_opportunity', 'opportunity_observation_notes', ['opportunity_type', 'opportunity_id'])
    op.create_index('ix_opportunity_observation_notes_note_type', 'opportunity_observation_notes', ['note_type'])
    op.create_index('ix_opportunity_observation_notes_created_at', 'opportunity_observation_notes', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_opportunity_observation_notes_created_at', table_name='opportunity_observation_notes')
    op.drop_index('ix_opportunity_observation_notes_note_type', table_name='opportunity_observation_notes')
    op.drop_index('ix_opportunity_observation_notes_opportunity', table_name='opportunity_observation_notes')
    op.drop_table('opportunity_observation_notes')

    op.drop_index('ix_data_sync_plan_executions_created_at', table_name='data_sync_plan_executions')
    op.drop_index('ix_data_sync_plan_executions_status', table_name='data_sync_plan_executions')
    op.drop_index('ix_data_sync_plan_executions_plan_id', table_name='data_sync_plan_executions')
    op.drop_table('data_sync_plan_executions')

    op.drop_index('ix_discovery_plans_enabled', table_name='discovery_plans')
    op.drop_index('ix_discovery_plans_strategy_type', table_name='discovery_plans')
    op.drop_index('ix_discovery_plans_scope', table_name='discovery_plans')
    op.drop_table('discovery_plans')
