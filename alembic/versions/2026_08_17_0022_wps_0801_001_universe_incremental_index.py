"""wps_0801_001_universe_incremental_index.

Revision ID: wps_0801_001_universe_incremental_index
Revises: wps_0023_020_api_deprecation_logs
Create Date: 2026-08-17 00:22:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0801_001_universe_incremental_index'
down_revision = 'wps_0023_020_api_deprecation_logs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'universe_symbols',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol', String(32), nullable=False, unique=True),
        Column('name', String(128), nullable=True),
        Column('asset_type', String(16), nullable=False),
        Column('market', String(16), nullable=False),
        Column('region', String(8), nullable=False),
        Column('board', String(32), nullable=True),
        Column('industry', String(64), nullable=True),
        Column('listed_at', Date, nullable=True),
        Column('last_synced_at', DateTime, nullable=True),
        Column('last_bar_date', Date, nullable=True),
        Column('bar_count', Integer, nullable=False, server_default='0'),
        Column('is_synced', Integer, nullable=False, server_default='0'),
        Column('sync_failed', Integer, nullable=False, server_default='0'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_universe_symbols_symbol', 'universe_symbols', ['symbol'], unique=True)
    op.create_index('ix_universe_symbol_asset', 'universe_symbols', ['asset_type', 'region'])
    op.create_index('ix_universe_symbol_synced', 'universe_symbols', ['is_synced', 'last_bar_date'])
    op.create_index(
        'ix_universe_incremental_pending',
        'universe_symbols',
        ['region', 'asset_type', 'is_synced', 'last_bar_date', 'last_synced_at', 'sync_failed'],
    )

    op.create_table(
        'universe_daily_bars',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('universe_symbol_id', Integer, ForeignKey('universe_symbols.id', ondelete='CASCADE'), nullable=False),
        Column('trade_date', Date, nullable=False),
        Column('open', Float, nullable=True),
        Column('high', Float, nullable=True),
        Column('low', Float, nullable=True),
        Column('close', Float, nullable=True),
        Column('volume', Float, nullable=True),
        Column('amount', Float, nullable=True),
        Column('turnover_rate', Float, nullable=True),
        Column('source', String(32), nullable=False, server_default='akshare'),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('universe_symbol_id', 'trade_date', name='uq_universe_bar_symbol_date'),
    )
    op.create_index('ix_universe_bar_symbol_date', 'universe_daily_bars', ['universe_symbol_id', 'trade_date'])

    op.create_table(
        'discovery_candidates',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('scan_run_id', Integer, ForeignKey('scan_runs.id', ondelete='CASCADE'), nullable=False),
        Column('universe_symbol_id', Integer, ForeignKey('universe_symbols.id', ondelete='CASCADE'), nullable=False),
        Column('symbol', String(32), nullable=False),
        Column('name', String(128), nullable=True),
        Column('asset_type', String(16), nullable=True),
        Column('quality_score', Float, nullable=True),
        Column('timing_score', Float, nullable=True),
        Column('priority_score', Float, nullable=True),
        Column('dimension_scores_json', Text, nullable=True),
        Column('scoring_config_snapshot_json', Text, nullable=True),
        Column('stage', String(16), nullable=True),
        Column('action', String(16), nullable=True),
        Column('reason_tags', Text, nullable=True),
        Column('warning_days', Integer, nullable=False, server_default='3'),
        Column('valid_days', Integer, nullable=False, server_default='5'),
        Column('is_promoted', Integer, nullable=False, server_default='0'),
        Column('promoted_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('scan_run_id', 'universe_symbol_id', name='uq_candidate_run_symbol'),
    )
    op.create_index('ix_candidate_promoted', 'discovery_candidates', ['is_promoted', 'created_at'])
    op.create_index('ix_candidate_run', 'discovery_candidates', ['scan_run_id'])


def downgrade() -> None:
    op.drop_index('ix_candidate_run', table_name='discovery_candidates')
    op.drop_index('ix_candidate_promoted', table_name='discovery_candidates')
    op.drop_table('discovery_candidates')

    op.drop_index('ix_universe_bar_symbol_date', table_name='universe_daily_bars')
    op.drop_table('universe_daily_bars')

    op.drop_index('ix_universe_incremental_pending', table_name='universe_symbols')
    op.drop_index('ix_universe_symbol_synced', table_name='universe_symbols')
    op.drop_index('ix_universe_symbol_asset', table_name='universe_symbols')
    op.drop_index('ix_universe_symbols_symbol', table_name='universe_symbols')
    op.drop_table('universe_symbols')
