"""wps_0023_018_discovery_score_snapshots.

Revision ID: wps_0023_018_discovery_score_snapshots
Revises: wps_0023_017_ai_profiles
Create Date: 2026-08-17 00:19:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_018_discovery_score_snapshots'
down_revision = 'wps_0023_017_ai_profiles'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'discovery_score_snapshots',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('scope', String(32), nullable=False),
        Column('trade_date', DateTime, nullable=False),
        Column('status', String(16), nullable=False, server_default='building'),
        Column('scoring_config_id', Integer, nullable=True),
        Column('scoring_config_version', Integer, nullable=True),
        Column('weight_mode', String(16), nullable=True),
        Column('factor_model_run_id', String(64), nullable=True),
        Column('data_cutoff_at', DateTime, nullable=True),
        Column('generated_at', DateTime, nullable=True),
        Column('build_duration_seconds', Float, nullable=True),
        Column('source_task_id', String(64), nullable=True),
        Column('symbol_count', Integer, nullable=False, server_default='0'),
        Column('coverage_pct', Float, nullable=False, server_default='0.0'),
        Column('dirty_symbol_count', Integer, nullable=False, server_default='0'),
        Column('error_summary_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
        UniqueConstraint('scope', 'status', 'trade_date', name='uq_snapshot_scope_status_date'),
    )
    op.create_index('ix_discovery_score_snapshots_scope', 'discovery_score_snapshots', ['scope'])
    op.create_index('ix_discovery_score_snapshots_trade_date', 'discovery_score_snapshots', ['trade_date'])
    op.create_index('ix_discovery_score_snapshots_status', 'discovery_score_snapshots', ['status'])
    op.create_index('ix_discovery_score_snapshots_scoring_config_id', 'discovery_score_snapshots', ['scoring_config_id'])
    op.create_index('ix_discovery_score_snapshots_source_task_id', 'discovery_score_snapshots', ['source_task_id'])
    op.create_index('ix_snapshot_scope_status_date', 'discovery_score_snapshots', ['scope', 'status', 'trade_date', 'generated_at'])

    op.create_table(
        'discovery_score_snapshot_items',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('snapshot_id', Integer, ForeignKey('discovery_score_snapshots.id', ondelete='CASCADE'), nullable=False),
        Column('universe_symbol_id', Integer, nullable=True),
        Column('symbol_id', Integer, nullable=True),
        Column('quality_score', Float, nullable=True),
        Column('timing_score', Float, nullable=True),
        Column('priority_score', Float, nullable=True),
        Column('dimension_scores_json', Text, nullable=True),
        Column('stage', String(32), nullable=True),
        Column('action', String(32), nullable=True),
        Column('data_credibility', Float, nullable=True),
        Column('health_summary_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('snapshot_id', 'universe_symbol_id', name='uq_snapshot_item_symbol'),
    )
    op.create_index('ix_discovery_score_snapshot_items_snapshot_id', 'discovery_score_snapshot_items', ['snapshot_id'])
    op.create_index('ix_discovery_score_snapshot_items_universe_symbol_id', 'discovery_score_snapshot_items', ['universe_symbol_id'])
    op.create_index('ix_discovery_score_snapshot_items_symbol_id', 'discovery_score_snapshot_items', ['symbol_id'])
    op.create_index('ix_snapshot_item_priority', 'discovery_score_snapshot_items', ['snapshot_id', 'priority_score'])
    op.create_index('ix_snapshot_item_action_stage', 'discovery_score_snapshot_items', ['snapshot_id', 'action', 'stage', 'priority_score'])


def downgrade() -> None:
    op.drop_index('ix_snapshot_item_action_stage', table_name='discovery_score_snapshot_items')
    op.drop_index('ix_snapshot_item_priority', table_name='discovery_score_snapshot_items')
    op.drop_index('ix_discovery_score_snapshot_items_symbol_id', table_name='discovery_score_snapshot_items')
    op.drop_index('ix_discovery_score_snapshot_items_universe_symbol_id', table_name='discovery_score_snapshot_items')
    op.drop_index('ix_discovery_score_snapshot_items_snapshot_id', table_name='discovery_score_snapshot_items')
    op.drop_table('discovery_score_snapshot_items')

    op.drop_index('ix_snapshot_scope_status_date', table_name='discovery_score_snapshots')
    op.drop_index('ix_discovery_score_snapshots_source_task_id', table_name='discovery_score_snapshots')
    op.drop_index('ix_discovery_score_snapshots_scoring_config_id', table_name='discovery_score_snapshots')
    op.drop_index('ix_discovery_score_snapshots_status', table_name='discovery_score_snapshots')
    op.drop_index('ix_discovery_score_snapshots_trade_date', table_name='discovery_score_snapshots')
    op.drop_index('ix_discovery_score_snapshots_scope', table_name='discovery_score_snapshots')
    op.drop_table('discovery_score_snapshots')
