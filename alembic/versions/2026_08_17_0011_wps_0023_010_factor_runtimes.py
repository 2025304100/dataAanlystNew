"""wps_0023_010_factor_runtimes.

Revision ID: wps_0023_010_factor_runtimes
Revises: wps_0023_009_factor_models
Create Date: 2026-08-17 00:11:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_010_factor_runtimes'
down_revision = 'wps_0023_009_factor_models'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'opportunity_transition_events',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('event_type', String(32), nullable=False),
        Column('from_stage', String(16), nullable=True),
        Column('to_stage', String(16), nullable=False),
        Column('symbol_id', Integer, nullable=True),
        Column('portfolio_id', Integer, nullable=True),
        Column('scan_run_id', Integer, nullable=True),
        Column('scan_result_id', Integer, nullable=True),
        Column('watchlist_item_id', Integer, nullable=True),
        Column('triggered_by', String(32), nullable=False, server_default='user'),
        Column('reason', Text, nullable=True),
        Column('event_data_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_opportunity_transition_events_event_type', 'opportunity_transition_events', ['event_type'])
    op.create_index('ix_opportunity_transition_events_symbol_id', 'opportunity_transition_events', ['symbol_id'])
    op.create_index('ix_opportunity_transition_events_portfolio_id', 'opportunity_transition_events', ['portfolio_id'])
    op.create_index('ix_opportunity_transition_events_created_at', 'opportunity_transition_events', ['created_at'])

    op.create_table(
        'factor_evaluation_runs',
        Column('id', String(64), primary_key=True),
        Column('factor_version_id', Integer, ForeignKey('factor_versions.id', ondelete='CASCADE'), nullable=False),
        Column('universe_snapshot_id', String(64), nullable=True),
        Column('data_cutoff_at', DateTime, nullable=True),
        Column('target_code', String(32), nullable=True, server_default='target_5d_return'),
        Column('train_start_date', DateTime, nullable=True),
        Column('train_end_date', DateTime, nullable=True),
        Column('validation_start_date', DateTime, nullable=True),
        Column('validation_end_date', DateTime, nullable=True),
        Column('config_json', Text, nullable=False, server_default='{}'),
        Column('metrics_json', Text, nullable=False, server_default='{}'),
        Column('gate_result', String(24), nullable=True),
        Column('rejection_reasons_json', Text, nullable=True),
        Column('artifact_path', String(512), nullable=True),
        Column('task_id', String(64), nullable=True),
        Column('created_by', String(128), nullable=False, server_default='local_user'),
        Column('selected_trade_date', String(10), nullable=True),
        Column('observed_symbols', Integer, nullable=True),
        Column('expected_symbols', Integer, nullable=True),
        Column('completeness_ratio', Float, nullable=True),
        Column('fallback_reason', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_factor_evaluation_runs_factor_version_id', 'factor_evaluation_runs', ['factor_version_id'])
    op.create_index('ix_factor_evaluation_runs_data_cutoff_at', 'factor_evaluation_runs', ['data_cutoff_at'])
    op.create_index('ix_factor_evaluation_runs_gate_result', 'factor_evaluation_runs', ['gate_result'])
    op.create_index('ix_factor_evaluation_runs_task_id', 'factor_evaluation_runs', ['task_id'])
    op.create_index('ix_evaluation_runs_factor_cutoff', 'factor_evaluation_runs', ['factor_version_id', 'data_cutoff_at'])


def downgrade() -> None:
    op.drop_index('ix_evaluation_runs_factor_cutoff', table_name='factor_evaluation_runs')
    op.drop_index('ix_factor_evaluation_runs_task_id', table_name='factor_evaluation_runs')
    op.drop_index('ix_factor_evaluation_runs_gate_result', table_name='factor_evaluation_runs')
    op.drop_index('ix_factor_evaluation_runs_data_cutoff_at', table_name='factor_evaluation_runs')
    op.drop_index('ix_factor_evaluation_runs_factor_version_id', table_name='factor_evaluation_runs')
    op.drop_table('factor_evaluation_runs')

    op.drop_index('ix_opportunity_transition_events_created_at', table_name='opportunity_transition_events')
    op.drop_index('ix_opportunity_transition_events_portfolio_id', table_name='opportunity_transition_events')
    op.drop_index('ix_opportunity_transition_events_symbol_id', table_name='opportunity_transition_events')
    op.drop_index('ix_opportunity_transition_events_event_type', table_name='opportunity_transition_events')
    op.drop_table('opportunity_transition_events')
