"""wps_0023_011_factor_evaluations.

Revision ID: wps_0023_011_factor_evaluations
Revises: wps_0023_010_factor_runtimes
Create Date: 2026-08-17 00:12:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_011_factor_evaluations'
down_revision = 'wps_0023_010_factor_runtimes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'factor_transition_audits',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('factor_id', Integer, ForeignKey('factors.id', ondelete='CASCADE'), nullable=False),
        Column('factor_version_id', Integer, ForeignKey('factor_versions.id', ondelete='SET NULL'), nullable=True),
        Column('from_status', String(32), nullable=True),
        Column('to_status', String(32), nullable=False),
        Column('actor', String(128), nullable=False, server_default='local_user'),
        Column('reason', Text, nullable=True),
        Column('evidence_run_id', String(64), ForeignKey('factor_evaluation_runs.id', ondelete='SET NULL'), nullable=True),
        Column('request_id', String(64), nullable=True),
        Column('migration_note', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_factor_transition_audits_factor_id', 'factor_transition_audits', ['factor_id'])
    op.create_index('ix_factor_transition_audits_created_at', 'factor_transition_audits', ['created_at'])
    op.create_index('ix_transition_audits_factor_created', 'factor_transition_audits', ['factor_id', 'created_at'])

    op.create_table(
        'factor_sets',
        Column('id', String(64), primary_key=True),
        Column('name', String(128), nullable=False),
        Column('description', Text, nullable=True),
        Column('content_hash', String(64), nullable=True),
        Column('status', String(32), nullable=False, server_default='draft'),
        Column('frozen_at', DateTime, nullable=True),
        Column('created_by', String(128), nullable=False, server_default='local_user'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_factor_sets_content_hash', 'factor_sets', ['content_hash'])
    op.create_index('ix_factor_sets_status', 'factor_sets', ['status'])

    op.create_table(
        'factor_set_members',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('factor_set_id', String(64), ForeignKey('factor_sets.id', ondelete='CASCADE'), nullable=False),
        Column('factor_id', Integer, ForeignKey('factors.id', ondelete='CASCADE'), nullable=False),
        Column('factor_version_id', Integer, ForeignKey('factor_versions.id', ondelete='CASCADE'), nullable=False),
        Column('factor_code', String(64), nullable=False),
        Column('factor_version', Integer, nullable=False),
        Column('role', String(32), nullable=False, server_default='feature'),
        Column('weight_constraint', String(32), nullable=True),
        Column('display_order', Integer, nullable=False, server_default='0'),
        Column('missing_policy', String(32), nullable=False, server_default='exclude'),
        Column('excluded_reason', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('factor_set_id', 'factor_id', name='uq_factor_set_member'),
    )
    op.create_index('ix_factor_set_members_factor_set_id', 'factor_set_members', ['factor_set_id'])

    op.create_table(
        'factor_shadow_observations',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('factor_id', Integer, ForeignKey('factors.id', ondelete='CASCADE'), nullable=False),
        Column('factor_version_id', Integer, ForeignKey('factor_versions.id', ondelete='CASCADE'), nullable=False),
        Column('trade_date', String(10), nullable=False),
        Column('observed_symbols', Integer, nullable=True),
        Column('expected_symbols', Integer, nullable=True),
        Column('completeness_ratio', Float, nullable=True),
        Column('is_valid_day', Integer, nullable=False, server_default='1'),
        Column('invalid_reason', String(64), nullable=True),
        Column('ic_value', Float, nullable=True),
        Column('coverage', Float, nullable=True),
        Column('turnover', Float, nullable=True),
        Column('metrics_json', Text, nullable=False, server_default='{}'),
        Column('health_status', String(24), nullable=False, server_default='healthy'),
        Column('health_reason', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('factor_version_id', 'trade_date', name='uq_shadow_observation_version_date'),
    )
    op.create_index('ix_factor_shadow_observations_factor_id', 'factor_shadow_observations', ['factor_id'])
    op.create_index('ix_factor_shadow_observations_trade_date', 'factor_shadow_observations', ['trade_date'])
    op.create_index('ix_shadow_obs_factor_trade', 'factor_shadow_observations', ['factor_id', 'trade_date'])


def downgrade() -> None:
    op.drop_index('ix_shadow_obs_factor_trade', table_name='factor_shadow_observations')
    op.drop_index('ix_factor_shadow_observations_trade_date', table_name='factor_shadow_observations')
    op.drop_index('ix_factor_shadow_observations_factor_id', table_name='factor_shadow_observations')
    op.drop_table('factor_shadow_observations')

    op.drop_index('ix_factor_set_members_factor_set_id', table_name='factor_set_members')
    op.drop_table('factor_set_members')

    op.drop_index('ix_factor_sets_status', table_name='factor_sets')
    op.drop_index('ix_factor_sets_content_hash', table_name='factor_sets')
    op.drop_table('factor_sets')

    op.drop_index('ix_transition_audits_factor_created', table_name='factor_transition_audits')
    op.drop_index('ix_factor_transition_audits_created_at', table_name='factor_transition_audits')
    op.drop_index('ix_factor_transition_audits_factor_id', table_name='factor_transition_audits')
    op.drop_table('factor_transition_audits')
