"""wps_0023_003_daily_bar.

Revision ID: wps_0023_003_daily_bar
Revises: wps_0023_002_symbols
Create Date: 2026-08-17 00:04:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_003_daily_bar'
down_revision = 'wps_0023_002_symbols'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'daily_bars',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('trade_date', Date, nullable=False),
        Column('open', Float, nullable=False),
        Column('high', Float, nullable=False),
        Column('low', Float, nullable=False),
        Column('close', Float, nullable=False),
        Column('volume', Float, nullable=True),
        Column('amount', Float, nullable=True),
        Column('turnover_rate', Float, nullable=True),
        Column('source', String(32), nullable=False, server_default='akshare'),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('symbol_id', 'trade_date', name='uq_daily_bar_symbol_date'),
    )
    op.create_index('ix_daily_bars_symbol_id', 'daily_bars', ['symbol_id'])
    op.create_index('ix_daily_bars_trade_date', 'daily_bars', ['trade_date'])

    op.create_table(
        'scoring_configs',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('asset_type', String(16), nullable=False),
        Column('preset_key', String(64), nullable=False),
        Column('name', String(128), nullable=False),
        Column('description', Text, nullable=True),
        Column('version', Integer, nullable=False, server_default='1'),
        Column('config_json', Text, nullable=False),
        Column('preset_source', String(16), nullable=False, server_default='system'),
        Column('is_system', Integer, nullable=False, server_default='0'),
        Column('is_active', Integer, nullable=False, server_default='0'),
        Column('is_latest', Integer, nullable=False, server_default='1'),
        Column('base_preset_key', String(64), nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
        UniqueConstraint('asset_type', 'preset_key', 'version', name='uq_scoring_config_type_key_version'),
    )
    op.create_index('ix_scoring_configs_asset_type', 'scoring_configs', ['asset_type'])
    op.create_index('ix_scoring_configs_preset_key', 'scoring_configs', ['preset_key'])
    op.create_index('ix_scoring_configs_is_system', 'scoring_configs', ['is_system'])
    op.create_index('ix_scoring_configs_is_active', 'scoring_configs', ['is_active'])
    op.create_index('ix_scoring_configs_is_latest', 'scoring_configs', ['is_latest'])


def downgrade() -> None:
    op.drop_index('ix_scoring_configs_is_latest', table_name='scoring_configs')
    op.drop_index('ix_scoring_configs_is_active', table_name='scoring_configs')
    op.drop_index('ix_scoring_configs_is_system', table_name='scoring_configs')
    op.drop_index('ix_scoring_configs_preset_key', table_name='scoring_configs')
    op.drop_index('ix_scoring_configs_asset_type', table_name='scoring_configs')
    op.drop_table('scoring_configs')

    op.drop_index('ix_daily_bars_trade_date', table_name='daily_bars')
    op.drop_index('ix_daily_bars_symbol_id', table_name='daily_bars')
    op.drop_table('daily_bars')
