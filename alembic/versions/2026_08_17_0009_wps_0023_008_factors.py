"""wps_0023_008_factors.

Revision ID: wps_0023_008_factors
Revises: wps_0023_007_scans
Create Date: 2026-08-17 00:09:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_008_factors'
down_revision = 'wps_0023_007_scans'
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _drop_table_if_exists(table_name: str) -> None:
    """Drop an optional metadata-first table without assuming migration order."""
    if _table_exists(table_name):
        op.drop_table(table_name)


def upgrade() -> None:
    op.create_table(
        'investment_themes',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(128), nullable=False, unique=True),
        Column('description', Text, nullable=True),
        Column('tags_json', Text, nullable=True),
        Column('is_active', Integer, nullable=False, server_default='1'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_investment_themes_is_active', 'investment_themes', ['is_active'])

    op.create_table(
        'signal_rules',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, nullable=False),
        Column('rule_name', String(128), nullable=False),
        Column('rule_type', String(32), nullable=False),
        Column('enabled', Integer, nullable=False, server_default='1'),
        Column('config_json', Text, nullable=False),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_signal_rules_portfolio_id', 'signal_rules', ['portfolio_id'])
    op.create_index('ix_signal_rules_rule_type', 'signal_rules', ['rule_type'])
    op.create_index('ix_signal_rules_enabled', 'signal_rules', ['enabled'])

    op.create_table(
        'scheduled_tasks',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('task_name', String(128), nullable=False, unique=True),
        Column('task_type', String(32), nullable=False),
        Column('cron_expression', String(64), nullable=False),
        Column('enabled', Integer, nullable=False, server_default='1'),
        Column('last_run_at', DateTime, nullable=True),
        Column('next_run_at', DateTime, nullable=True),
        Column('config_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_scheduled_tasks_task_type', 'scheduled_tasks', ['task_type'])
    op.create_index('ix_scheduled_tasks_enabled', 'scheduled_tasks', ['enabled'])

    op.create_table(
        'backtest_runs',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, nullable=False),
        Column('run_name', String(128), nullable=False),
        Column('status', String(16), nullable=False, server_default='pending'),
        Column('start_date', String(32), nullable=True),
        Column('end_date', String(32), nullable=True),
        Column('config_json', Text, nullable=True),
        Column('metrics_json', Text, nullable=True),
        Column('started_at', DateTime, nullable=True),
        Column('finished_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_backtest_runs_portfolio_id', 'backtest_runs', ['portfolio_id'])
    op.create_index('ix_backtest_runs_status', 'backtest_runs', ['status'])

    op.create_table(
        'market_events',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('event_type', String(32), nullable=False),
        Column('event_date', String(32), nullable=False),
        Column('title', String(256), nullable=False),
        Column('content', Text, nullable=True),
        Column('symbol_id', Integer, nullable=True),
        Column('impact_level', String(16), nullable=True),
        Column('source', String(64), nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_market_events_event_type', 'market_events', ['event_type'])
    op.create_index('ix_market_events_event_date', 'market_events', ['event_date'])
    op.create_index('ix_market_events_symbol_id', 'market_events', ['symbol_id'])


def downgrade() -> None:
    # Current metadata may have created these dependent theme tables before
    # this historical revision is replayed.  Remove them first so SQLite does
    # not leave FKs that reference a market_events table we are about to drop.
    _drop_table_if_exists('theme_catalysts')
    _drop_table_if_exists('symbol_theme_mappings')

    if _table_exists('market_events'):
        op.drop_index('ix_market_events_symbol_id', table_name='market_events')
        op.drop_index('ix_market_events_event_date', table_name='market_events')
        op.drop_index('ix_market_events_event_type', table_name='market_events')
        op.drop_table('market_events')

    op.drop_index('ix_backtest_runs_status', table_name='backtest_runs')
    op.drop_index('ix_backtest_runs_portfolio_id', table_name='backtest_runs')
    op.drop_table('backtest_runs')

    op.drop_index('ix_scheduled_tasks_enabled', table_name='scheduled_tasks')
    op.drop_index('ix_scheduled_tasks_task_type', table_name='scheduled_tasks')
    op.drop_table('scheduled_tasks')

    op.drop_index('ix_signal_rules_enabled', table_name='signal_rules')
    op.drop_index('ix_signal_rules_rule_type', table_name='signal_rules')
    op.drop_index('ix_signal_rules_portfolio_id', table_name='signal_rules')
    op.drop_table('signal_rules')

    if _table_exists('investment_themes'):
        op.drop_index('ix_investment_themes_is_active', table_name='investment_themes')
        op.drop_table('investment_themes')
