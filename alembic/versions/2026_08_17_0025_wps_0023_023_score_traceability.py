"""wps_0023_023_score_traceability.

Revision ID: wps_0023_023_score_traceability
Revises: wps_0023_022_shadow_observations
Create Date: 2026-08-17 00:25:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_023_score_traceability'
down_revision = 'wps_0023_022_shadow_observations'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'portfolio_equity_snapshots',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='CASCADE'), nullable=False),
        Column('snapshot_date', Date, nullable=False),
        Column('cash_balance', Float, nullable=False, server_default='0'),
        Column('market_value', Float, nullable=False, server_default='0'),
        Column('total_equity', Float, nullable=False, server_default='0'),
        Column('realized_pnl', Float, nullable=False, server_default='0'),
        Column('unrealized_pnl', Float, nullable=False, server_default='0'),
        Column('daily_return', Float, nullable=False, server_default='0'),
        Column('position_count', Integer, nullable=False, server_default='0'),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('portfolio_id', 'snapshot_date', name='uq_portfolio_equity_snapshot_date'),
    )
    op.create_index('ix_portfolio_equity_snapshots_portfolio_id', 'portfolio_equity_snapshots', ['portfolio_id'])
    op.create_index('ix_portfolio_equity_snapshots_snapshot_date', 'portfolio_equity_snapshots', ['snapshot_date'])

    op.create_table(
        'index_prices',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol', String(16), nullable=False),
        Column('trade_date', Date, nullable=False),
        Column('open', Float, nullable=False),
        Column('high', Float, nullable=False),
        Column('low', Float, nullable=False),
        Column('close', Float, nullable=False),
        Column('volume', Float, nullable=True),
        Column('amount', Float, nullable=True),
        Column('source', String(32), nullable=False, server_default='akshare'),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('symbol', 'trade_date', name='uq_index_price_symbol_date'),
    )
    op.create_index('ix_index_prices_symbol', 'index_prices', ['symbol'])
    op.create_index('ix_index_prices_trade_date', 'index_prices', ['trade_date'])

    op.create_table(
        'akshare_api_config',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('api_key', String(64), nullable=False, unique=True),
        Column('enabled', Integer, nullable=False, server_default='1'),
        Column('anti_risk_strategy', String(16), nullable=False, server_default='standard'),
        Column('delay_min_ms', Integer, nullable=False, server_default='300'),
        Column('delay_max_ms', Integer, nullable=False, server_default='800'),
        Column('last_probe_at', DateTime, nullable=True),
        Column('last_probe_success', Integer, nullable=True),
        Column('last_probe_latency_ms', Integer, nullable=True),
        Column('last_probe_error', Text, nullable=True),
        Column('last_call_at', DateTime, nullable=True),
        Column('last_call_success', Integer, nullable=True),
        Column('last_call_error', Text, nullable=True),
        Column('total_calls', Integer, nullable=False, server_default='0'),
        Column('total_failures', Integer, nullable=False, server_default='0'),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_akshare_api_config_api_key', 'akshare_api_config', ['api_key'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_akshare_api_config_api_key', table_name='akshare_api_config')
    op.drop_table('akshare_api_config')

    op.drop_index('ix_index_prices_trade_date', table_name='index_prices')
    op.drop_index('ix_index_prices_symbol', table_name='index_prices')
    op.drop_table('index_prices')

    op.drop_index('ix_portfolio_equity_snapshots_snapshot_date', table_name='portfolio_equity_snapshots')
    op.drop_index('ix_portfolio_equity_snapshots_portfolio_id', table_name='portfolio_equity_snapshots')
    op.drop_table('portfolio_equity_snapshots')
