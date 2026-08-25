"""wps_0023_001_scan_runs_cache.

Revision ID: wps_0023_001_scan_runs_cache
Revises: wps_001_external_endpoint
Create Date: 2026-08-17 00:02:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_001_scan_runs_cache'
down_revision = 'wps_001_external_endpoint'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'symbols',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol', String(32), nullable=False, unique=True),
        Column('name', String(128), nullable=False),
        Column('asset_type', String(16), nullable=False),
        Column('market', String(16), nullable=False),
        Column('board', String(32), nullable=True),
        Column('industry', String(64), nullable=True),
        Column('theme', String(64), nullable=True),
        Column('is_st', Integer, nullable=False, server_default='0'),
        Column('is_active', Integer, nullable=False, server_default='1'),
        Column('listed_at', String(32), nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_symbols_symbol', 'symbols', ['symbol'], unique=True)
    op.create_index('ix_symbols_asset_type', 'symbols', ['asset_type'])
    op.create_index('ix_symbols_market', 'symbols', ['market'])
    op.create_index('ix_symbols_theme', 'symbols', ['theme'])

    op.create_table(
        'scan_presets',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(128), nullable=False, unique=True),
        Column('scope_type', String(16), nullable=False),
        Column('markets', Text, nullable=True),
        Column('boards', Text, nullable=True),
        Column('filters_json', Text, nullable=True),
        Column('sort_mode', String(32), nullable=True),
        Column('description', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )

    op.create_table(
        'portfolios',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(128), nullable=False, unique=True),
        Column('account_type', String(16), nullable=False),
        Column('asset_scope', String(16), nullable=False, server_default='mixed'),
        Column('total_capital', Float, nullable=False),
        Column('investable_ratio', Float, nullable=False),
        Column('cash_reserve_ratio', Float, nullable=False),
        Column('currency', String(16), nullable=False, server_default='CNY'),
        Column('is_default', Integer, nullable=False, server_default='0'),
        Column('auto_trade_enabled', Integer, nullable=False, server_default='0'),
        Column('auto_trade_last_run_at', DateTime, nullable=True),
        Column('auto_trade_source_mode', String(32), nullable=False, server_default='portfolio'),
        Column('buy_fee_pct', Float, nullable=False, server_default='0.00025'),
        Column('sell_fee_pct', Float, nullable=False, server_default='0.00025'),
        Column('benchmark_code', String(32), nullable=False, server_default='000300'),
        Column('default_single_position_pct', Float, nullable=False, server_default='0.30'),
        Column('is_test', Integer, nullable=False, server_default='0'),
        Column('key_members_json', Text, nullable=True),
        Column('last_successful_trade_date', String(32), nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_portfolios_asset_scope', 'portfolios', ['asset_scope'])

    op.create_table(
        'scan_runs',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('preset_id', Integer, ForeignKey('scan_presets.id', ondelete='SET NULL'), nullable=True),
        Column('run_name', String(128), nullable=False),
        Column('scope_snapshot', Text, nullable=False),
        Column('filters_snapshot', Text, nullable=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='SET NULL'), nullable=True),
        Column('portfolio_rule_id', Integer, nullable=True),
        Column('status', String(16), nullable=False),
        Column('started_at', DateTime, nullable=True),
        Column('finished_at', DateTime, nullable=True),
        Column('snapshot_id', Integer, nullable=True),
        Column('cache_key', String(128), nullable=True),
        Column('cache_hit', Integer, nullable=True),
        Column('total_in_snapshot', Integer, nullable=True),
        Column('coarse_match_count', Integer, nullable=True),
        Column('advanced_match_count', Integer, nullable=True),
        Column('result_rows_written', Integer, nullable=True),
        Column('degraded_reason', String(64), nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_scan_runs_status', 'scan_runs', ['status'])
    op.create_index('ix_scan_runs_snapshot_id', 'scan_runs', ['snapshot_id'])
    op.create_index('ix_scan_runs_cache_key', 'scan_runs', ['cache_key'])

    op.create_table(
        'scan_results',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('scan_run_id', Integer, ForeignKey('scan_runs.id', ondelete='CASCADE'), nullable=False),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('result_type', String(16), nullable=False),
        Column('rank_no', Integer, nullable=False),
        Column('quality_score', Float, nullable=True),
        Column('timing_score', Float, nullable=True),
        Column('priority_score', Float, nullable=True),
        Column('stage', String(16), nullable=True),
        Column('action', String(16), nullable=True),
        Column('recommended_position_pct', Float, nullable=True),
        Column('is_sector_overweight', Integer, nullable=False, server_default='0'),
        Column('is_asset_overweight', Integer, nullable=False, server_default='0'),
        Column('reason_tags', Text, nullable=True),
        Column('warning_days', Integer, nullable=False, server_default='3'),
        Column('valid_days', Integer, nullable=False, server_default='5'),
        Column('is_frozen', Integer, nullable=False, server_default='0'),
        Column('is_active', Integer, nullable=False, server_default='1'),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_scan_results_scan_run_id', 'scan_results', ['scan_run_id'])
    op.create_index('ix_scan_results_symbol_id', 'scan_results', ['symbol_id'])
    op.create_index('ix_scan_results_result_type', 'scan_results', ['result_type'])
    op.create_index('ix_scan_results_is_frozen', 'scan_results', ['is_frozen'])
    op.create_index('ix_scan_results_is_active', 'scan_results', ['is_active'])


def downgrade() -> None:
    op.drop_index('ix_scan_results_is_active', table_name='scan_results')
    op.drop_index('ix_scan_results_is_frozen', table_name='scan_results')
    op.drop_index('ix_scan_results_result_type', table_name='scan_results')
    op.drop_index('ix_scan_results_symbol_id', table_name='scan_results')
    op.drop_index('ix_scan_results_scan_run_id', table_name='scan_results')
    op.drop_table('scan_results')

    op.drop_index('ix_scan_runs_cache_key', table_name='scan_runs')
    op.drop_index('ix_scan_runs_snapshot_id', table_name='scan_runs')
    op.drop_index('ix_scan_runs_status', table_name='scan_runs')
    op.drop_table('scan_runs')

    op.drop_index('ix_portfolios_asset_scope', table_name='portfolios')
    op.drop_table('portfolios')

    op.drop_table('scan_presets')

    op.drop_index('ix_symbols_theme', table_name='symbols')
    op.drop_index('ix_symbols_market', table_name='symbols')
    op.drop_index('ix_symbols_asset_type', table_name='symbols')
    op.drop_index('ix_symbols_symbol', table_name='symbols')
    op.drop_table('symbols')
