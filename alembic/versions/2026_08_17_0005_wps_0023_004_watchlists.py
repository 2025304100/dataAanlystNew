"""wps_0023_004_watchlists.

Revision ID: wps_0023_004_watchlists
Revises: wps_0023_003_daily_bar
Create Date: 2026-08-17 00:05:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_004_watchlists'
down_revision = 'wps_0023_003_daily_bar'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'watchlists',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(128), nullable=False, unique=True),
        Column('list_type', String(32), nullable=False),
        Column('description', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )

    op.create_table(
        'watchlist_items',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('watchlist_id', Integer, ForeignKey('watchlists.id', ondelete='CASCADE'), nullable=False),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('note', Text, nullable=True),
        Column('score_snapshot_json', Text, nullable=True),
        Column('added_at', DateTime, nullable=False),
        Column('origin_type', String(32), nullable=False, server_default='manual'),
        Column('origin_id', Integer, nullable=True),
        Column('reason_json', Text, nullable=True),
        Column('status', String(16), nullable=False, server_default='watching'),
        Column('priority', Integer, nullable=False, server_default='0'),
        Column('tags_json', Text, nullable=True),
        Column('target_portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='SET NULL'), nullable=True),
        Column('updated_at', DateTime, nullable=True),
        Column('archived_at', DateTime, nullable=True),
        UniqueConstraint('watchlist_id', 'symbol_id', name='uq_watchlist_symbol'),
    )
    op.create_index('ix_watchlist_items_origin_id', 'watchlist_items', ['origin_id'])
    op.create_index('ix_watchlist_items_target_portfolio_id', 'watchlist_items', ['target_portfolio_id'])

    op.create_table(
        'scores',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('trade_date', String(32), nullable=False),
        Column('quality_score', Float, nullable=False),
        Column('quality_grade', String(1), nullable=False),
        Column('timing_score', Float, nullable=False),
        Column('stage', String(16), nullable=False),
        Column('action', String(16), nullable=False),
        Column('priority_score', Float, nullable=False),
        Column('trend_score', Float, nullable=True),
        Column('momentum_score', Float, nullable=True),
        Column('volatility_score', Float, nullable=True),
        Column('liquidity_score', Float, nullable=True),
        Column('breadth_score', Float, nullable=True),
        Column('event_score', Float, nullable=True),
        Column('breakout_score', Float, nullable=True),
        Column('pullback_score', Float, nullable=True),
        Column('overheat_penalty', Float, nullable=True),
        Column('data_credibility', Float, nullable=True),
        Column('scoring_asset_type', String(16), nullable=True),
        Column('scoring_config_id', Integer, nullable=True),
        Column('scoring_preset_key', String(64), nullable=True),
        Column('scoring_preset_name', String(128), nullable=True),
        Column('scoring_config_version', Integer, nullable=True),
        Column('scoring_config_snapshot_json', Text, nullable=True),
        Column('dimension_scores_json', Text, nullable=True),
        Column('factor_scores_json', Text, nullable=True),
        Column('weight_mode', String(16), nullable=False, server_default='manual'),
        Column('factor_model_run_id', String(64), nullable=True),
        Column('factor_data_cutoff_at', DateTime, nullable=True),
        Column('factor_set_id', String(64), nullable=True),
        Column('factor_member_versions_json', Text, nullable=True),
        Column('factor_quality_score', Float, nullable=True),
        Column('factor_timing_score', Float, nullable=True),
        Column('model_alpha_score', Float, nullable=True),
        Column('macro_regime', String(24), nullable=True),
        Column('macro_position_multiplier', Float, nullable=True),
        Column('calc_batch_id', String(64), nullable=False, server_default=''),
        Column('published_at', DateTime, nullable=True),
        Column('first_published_at', DateTime, nullable=True),
        Column('revision_at', DateTime, nullable=True),
        Column('pit_flag', String(16), nullable=False, server_default='NOT_CHECKED'),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('symbol_id', 'trade_date', 'calc_batch_id', name='uq_score_symbol_date_batch'),
    )
    op.create_index('ix_scores_symbol_id', 'scores', ['symbol_id'])
    op.create_index('ix_scores_trade_date', 'scores', ['trade_date'])
    op.create_index('ix_scores_stage', 'scores', ['stage'])
    op.create_index('ix_scores_action', 'scores', ['action'])
    op.create_index('ix_scores_scoring_asset_type', 'scores', ['scoring_asset_type'])
    op.create_index('ix_scores_scoring_config_id', 'scores', ['scoring_config_id'])
    op.create_index('ix_scores_weight_mode', 'scores', ['weight_mode'])
    op.create_index('ix_scores_factor_model_run_id', 'scores', ['factor_model_run_id'])
    op.create_index('ix_scores_factor_set_id', 'scores', ['factor_set_id'])
    op.create_index('ix_scores_published_at', 'scores', ['published_at'])


def downgrade() -> None:
    op.drop_index('ix_scores_published_at', table_name='scores')
    op.drop_index('ix_scores_factor_set_id', table_name='scores')
    op.drop_index('ix_scores_factor_model_run_id', table_name='scores')
    op.drop_index('ix_scores_weight_mode', table_name='scores')
    op.drop_index('ix_scores_scoring_config_id', table_name='scores')
    op.drop_index('ix_scores_scoring_asset_type', table_name='scores')
    op.drop_index('ix_scores_action', table_name='scores')
    op.drop_index('ix_scores_stage', table_name='scores')
    op.drop_index('ix_scores_trade_date', table_name='scores')
    op.drop_index('ix_scores_symbol_id', table_name='scores')
    op.drop_table('scores')

    op.drop_index('ix_watchlist_items_target_portfolio_id', table_name='watchlist_items')
    op.drop_index('ix_watchlist_items_origin_id', table_name='watchlist_items')
    op.drop_table('watchlist_items')

    op.drop_table('watchlists')
