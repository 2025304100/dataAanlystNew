"""wps_0023_002_symbols.

Revision ID: wps_0023_002_symbols
Revises: wps_0023_001_scan_runs_cache
Create Date: 2026-08-17 00:03:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_002_symbols'
down_revision = 'wps_0023_001_scan_runs_cache'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'alert_rules',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(120), nullable=False, server_default=''),
        Column('alert_type', String(32), nullable=False),
        Column('enabled', Integer, nullable=False, server_default='1'),
        Column('severity', String(16), nullable=False, server_default='warn'),
        Column('config_json', Text, nullable=True),
        Column('last_triggered_at', DateTime, nullable=True),
        Column('cooldown_minutes', Integer, nullable=False, server_default='60'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_alert_rules_alert_type', 'alert_rules', ['alert_type'])

    op.create_table(
        'alert_events',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('rule_id', Integer, nullable=False),
        Column('alert_type', String(32), nullable=False),
        Column('severity', String(16), nullable=False, server_default='warn'),
        Column('title', String(200), nullable=False, server_default=''),
        Column('message', Text, nullable=False, server_default=''),
        Column('symbol_id', Integer, nullable=True),
        Column('data_json', Text, nullable=True),
        Column('acknowledged', Integer, nullable=False, server_default='0'),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_alert_events_rule_id', 'alert_events', ['rule_id'])
    op.create_index('ix_alert_events_alert_type', 'alert_events', ['alert_type'])
    op.create_index('ix_alert_events_symbol_id', 'alert_events', ['symbol_id'])
    op.create_index('ix_alert_events_created_at', 'alert_events', ['created_at'])

    op.create_table(
        'factors',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('code', String(64), nullable=False, unique=True),
        Column('name', String(128), nullable=False),
        Column('category', String(64), nullable=False),
        Column('direction', String(16), nullable=False),
        Column('status', String(16), nullable=False),
        Column('source_type', String(32), nullable=True),
        Column('frequency', String(16), nullable=True),
        Column('default_missing_policy', String(32), nullable=False, server_default='exclude'),
        Column('is_active', Integer, nullable=False, server_default='1'),
        Column('description', Text, nullable=True),
        Column('formula_expr', Text, nullable=True),
        Column('origin', String(32), nullable=True),
        Column('lifecycle_status', String(32), nullable=True),
        Column('owner', String(128), nullable=True),
        Column('thesis', Text, nullable=True),
        Column('factor_kind', String(32), nullable=True),
        Column('asset_scope_json', Text, nullable=True),
        Column('active_version_id', Integer, nullable=True),
        Column('shadow_version_id', Integer, nullable=True),
        Column('risk_level', String(16), nullable=True),
        Column('archived_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_factors_code', 'factors', ['code'], unique=True)
    op.create_index('ix_factors_is_active', 'factors', ['is_active'])
    op.create_index('ix_factors_origin', 'factors', ['origin'])
    op.create_index('ix_factors_lifecycle_status', 'factors', ['lifecycle_status'])

    op.create_table(
        'factor_values',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('factor_id', Integer, ForeignKey('factors.id', ondelete='CASCADE'), nullable=False),
        Column('trade_date', Date, nullable=False),
        Column('raw_value', Float, nullable=True),
        Column('normalized_value', Float, nullable=True),
        Column('calc_batch_id', String(64), nullable=False, server_default=''),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_factor_values_symbol_id', 'factor_values', ['symbol_id'])
    op.create_index('ix_factor_values_factor_id', 'factor_values', ['factor_id'])
    op.create_index('ix_factor_values_trade_date', 'factor_values', ['trade_date'])


def downgrade() -> None:
    op.drop_index('ix_factor_values_trade_date', table_name='factor_values')
    op.drop_index('ix_factor_values_factor_id', table_name='factor_values')
    op.drop_index('ix_factor_values_symbol_id', table_name='factor_values')
    op.drop_table('factor_values')

    op.drop_index('ix_factors_lifecycle_status', table_name='factors')
    op.drop_index('ix_factors_origin', table_name='factors')
    op.drop_index('ix_factors_is_active', table_name='factors')
    op.drop_index('ix_factors_code', table_name='factors')
    op.drop_table('factors')

    op.drop_index('ix_alert_events_created_at', table_name='alert_events')
    op.drop_index('ix_alert_events_symbol_id', table_name='alert_events')
    op.drop_index('ix_alert_events_alert_type', table_name='alert_events')
    op.drop_index('ix_alert_events_rule_id', table_name='alert_events')
    op.drop_table('alert_events')

    op.drop_index('ix_alert_rules_alert_type', table_name='alert_rules')
    op.drop_table('alert_rules')
