"""wps_0023_005_trade_setups.

Revision ID: wps_0023_005_trade_setups
Revises: wps_0023_004_watchlists
Create Date: 2026-08-17 00:06:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_005_trade_setups'
down_revision = 'wps_0023_004_watchlists'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'trade_setups',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='CASCADE'), nullable=False),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('score_id', Integer, ForeignKey('scores.id', ondelete='CASCADE'), nullable=False),
        Column('scan_run_id', Integer, ForeignKey('scan_runs.id', ondelete='SET NULL'), nullable=True),
        Column('stage', String(16), nullable=False),
        Column('action', String(16), nullable=False),
        Column('entry_min', Float, nullable=True),
        Column('entry_max', Float, nullable=True),
        Column('stop_loss', Float, nullable=True),
        Column('target_price', Float, nullable=True),
        Column('recommended_position_pct', Float, nullable=False),
        Column('recommended_position_amount', Float, nullable=False),
        Column('risk_reward_ratio', Float, nullable=True),
        Column('allow_add_position', Integer, nullable=False, server_default='0'),
        Column('is_sector_overweight', Integer, nullable=False, server_default='0'),
        Column('is_asset_overweight', Integer, nullable=False, server_default='0'),
        Column('setup_reason', Text, nullable=True),
        Column('manual_overrides_json', Text, nullable=True),
        Column('field_sources_json', Text, nullable=True),
        Column('manual_tranche_plan_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_trade_setups_portfolio_id', 'trade_setups', ['portfolio_id'])
    op.create_index('ix_trade_setups_symbol_id', 'trade_setups', ['symbol_id'])
    op.create_index('ix_trade_setups_score_id', 'trade_setups', ['score_id'])
    op.create_index('ix_trade_setups_scan_run_id', 'trade_setups', ['scan_run_id'])

    op.create_table(
        'trade_signals',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='CASCADE'), nullable=False),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('trade_setup_id', Integer, ForeignKey('trade_setups.id', ondelete='CASCADE'), nullable=False),
        Column('signal_type', String(16), nullable=False),
        Column('signal_level', String(16), nullable=False),
        Column('message', Text, nullable=False),
        Column('trigger_price', Float, nullable=True),
        Column('status', String(16), nullable=False, server_default='active'),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_trade_signals_portfolio_id', 'trade_signals', ['portfolio_id'])
    op.create_index('ix_trade_signals_symbol_id', 'trade_signals', ['symbol_id'])
    op.create_index('ix_trade_signals_trade_setup_id', 'trade_signals', ['trade_setup_id'])

    op.create_table(
        'portfolio_rules',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='CASCADE'), nullable=False),
        Column('rule_name', String(128), nullable=False),
        Column('max_single_position_pct', Float, nullable=False),
        Column('max_sector_position_pct', Float, nullable=False),
        Column('max_stock_position_pct', Float, nullable=False),
        Column('max_etf_position_pct', Float, nullable=False),
        Column('max_loss_per_trade_pct', Float, nullable=False),
        Column('max_open_positions', Integer, nullable=False),
        Column('stage_limits_json', Text, nullable=False),
        Column('is_active', Integer, nullable=False, server_default='1'),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_portfolio_rules_portfolio_id', 'portfolio_rules', ['portfolio_id'])

    op.create_table(
        'positions',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='CASCADE'), nullable=False),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('quantity', Float, nullable=False, server_default='0'),
        Column('avg_cost', Float, nullable=False, server_default='0'),
        Column('latest_price', Float, nullable=False, server_default='0'),
        Column('market_value', Float, nullable=False, server_default='0'),
        Column('position_pct', Float, nullable=False, server_default='0'),
        Column('asset_type', String(16), nullable=False),
        Column('theme', String(64), nullable=True),
        Column('opened_at', DateTime, nullable=True),
        Column('updated_at', DateTime, nullable=False),
        UniqueConstraint('portfolio_id', 'symbol_id', name='uq_portfolio_symbol_position'),
    )
    op.create_index('ix_positions_portfolio_id', 'positions', ['portfolio_id'])
    op.create_index('ix_positions_symbol_id', 'positions', ['symbol_id'])


def downgrade() -> None:
    op.drop_index('ix_positions_symbol_id', table_name='positions')
    op.drop_index('ix_positions_portfolio_id', table_name='positions')
    op.drop_table('positions')

    op.drop_index('ix_portfolio_rules_portfolio_id', table_name='portfolio_rules')
    op.drop_table('portfolio_rules')

    op.drop_index('ix_trade_signals_trade_setup_id', table_name='trade_signals')
    op.drop_index('ix_trade_signals_symbol_id', table_name='trade_signals')
    op.drop_index('ix_trade_signals_portfolio_id', table_name='trade_signals')
    op.drop_table('trade_signals')

    op.drop_index('ix_trade_setups_scan_run_id', table_name='trade_setups')
    op.drop_index('ix_trade_setups_score_id', table_name='trade_setups')
    op.drop_index('ix_trade_setups_symbol_id', table_name='trade_setups')
    op.drop_index('ix_trade_setups_portfolio_id', table_name='trade_setups')
    op.drop_table('trade_setups')
