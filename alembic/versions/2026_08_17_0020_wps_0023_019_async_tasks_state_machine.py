"""wps_0023_019_async_tasks_state_machine.

Revision ID: wps_0023_019_async_tasks_state_machine
Revises: wps_0023_018_discovery_score_snapshots
Create Date: 2026-08-17 00:20:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_019_async_tasks_state_machine'
down_revision = 'wps_0023_018_discovery_score_snapshots'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'async_tasks',
        Column('id', String(64), primary_key=True),
        Column('task_type', String(32), nullable=False),
        Column('status', String(16), nullable=False),
        Column('stage', String(32), nullable=False),
        Column('percent', Float, nullable=False, server_default='0'),
        Column('message', Text, nullable=False, server_default=''),
        Column('total', Integer, nullable=False, server_default='0'),
        Column('processed', Integer, nullable=False, server_default='0'),
        Column('ok_count', Integer, nullable=False, server_default='0'),
        Column('failed_count', Integer, nullable=False, server_default='0'),
        Column('current_item', String(64), nullable=True),
        Column('payload_json', Text, nullable=True),
        Column('result_json', Text, nullable=True),
        Column('errors_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('started_at', DateTime, nullable=True),
        Column('finished_at', DateTime, nullable=True),
        Column('updated_at', DateTime, nullable=False),
        Column('heartbeat_at', DateTime, nullable=True),
        Column('stage_budget_seconds', Integer, nullable=True),
        Column('stage_started_at', DateTime, nullable=True),
        Column('last_progress_at', DateTime, nullable=True),
        Column('last_progress_percent', Float, nullable=True),
        Column('current_step_description', Text, nullable=True),
        Column('suggested_action', Text, nullable=True),
        Column('batch_recovery_json', Text, nullable=True),
        Column('last_patrol_at', DateTime, nullable=True),
        Column('worker_thread_id', String(32), nullable=True),
        Column('cancel_requested', Integer, nullable=True, server_default='0'),
    )
    op.create_index('ix_async_tasks_task_type', 'async_tasks', ['task_type'])
    op.create_index('ix_async_tasks_status', 'async_tasks', ['status'])
    op.create_index('ix_async_tasks_stage', 'async_tasks', ['stage'])
    op.create_index('ix_async_tasks_created_at', 'async_tasks', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_async_tasks_created_at', table_name='async_tasks')
    op.drop_index('ix_async_tasks_stage', table_name='async_tasks')
    op.drop_index('ix_async_tasks_status', table_name='async_tasks')
    op.drop_index('ix_async_tasks_task_type', table_name='async_tasks')
    op.drop_table('async_tasks')
