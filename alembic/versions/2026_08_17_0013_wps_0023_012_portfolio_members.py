"""wps_0023_012_portfolio_members.

Revision ID: wps_0023_012_portfolio_members
Revises: wps_0023_011_factor_evaluations
Create Date: 2026-08-17 00:13:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_012_portfolio_members'
down_revision = 'wps_0023_011_factor_evaluations'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'portfolio_members',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, nullable=False),
        Column('symbol_id', Integer, nullable=False),
        Column('status', String(16), nullable=False, server_default='active'),
        Column('execution_mode', String(16), nullable=False, server_default='manual'),
        Column('source_type', String(32), nullable=False, server_default='manual'),
        Column('source_id', Integer, nullable=True),
        Column('entry_rule_version_id', Integer, nullable=True),
        Column('exit_rule_version_id', Integer, nullable=True),
        Column('effective_from', DateTime, nullable=False),
        Column('effective_to', DateTime, nullable=True),
        Column('manual_lock', Integer, nullable=False, server_default='0'),
        Column('priority', Integer, nullable=False, server_default='0'),
        Column('note', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=True),
    )
    op.create_index('ix_portfolio_members_portfolio_id', 'portfolio_members', ['portfolio_id'])
    op.create_index('ix_portfolio_members_symbol_id', 'portfolio_members', ['symbol_id'])
    op.create_index('ix_portfolio_members_status', 'portfolio_members', ['status'])
    op.create_index('ix_portfolio_members_source_type', 'portfolio_members', ['source_type'])
    op.create_index('ix_portfolio_members_source_id', 'portfolio_members', ['source_id'])
    op.create_index('ix_portfolio_members_execution_mode', 'portfolio_members', ['execution_mode'])

    op.create_table(
        'portfolio_candidates',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='CASCADE'), nullable=False),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('effective_from', Date, nullable=False),
        Column('effective_to', Date, nullable=True),
        Column('auto_authorized_flag', Integer, nullable=False, server_default="1"),
        Column('removed_manually_flag', Integer, nullable=False, server_default="0"),
        Column('removal_reason', String(64), nullable=True),
        Column('audit_version', Integer, nullable=False, server_default="1"),
        Column('source_candidate_id', Integer, nullable=True),
        Column('source_type', String(16), nullable=True),
        Column('source_scan_run_id', Integer, nullable=True),
        Column('pool_memberships_json', Text, nullable=True),
        Column('admission_snapshot_json', Text, nullable=True),
        Column('priority_score', Float, nullable=True),
        Column('recommended_position_pct', Float, nullable=True),
        Column('factor_tag', String(128), nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
        UniqueConstraint('portfolio_id', 'symbol_id', 'effective_from', name='uq_portfolio_candidate_symbol_effective'),
    )
    op.create_index('ix_portfolio_candidates_portfolio_id', 'portfolio_candidates', ['portfolio_id'])
    op.create_index('ix_portfolio_candidates_symbol_id', 'portfolio_candidates', ['symbol_id'])


def downgrade() -> None:
    op.drop_index('ix_portfolio_candidates_symbol_id', table_name='portfolio_candidates')
    op.drop_index('ix_portfolio_candidates_portfolio_id', table_name='portfolio_candidates')
    op.drop_table('portfolio_candidates')

    op.drop_index('ix_portfolio_members_execution_mode', table_name='portfolio_members')
    op.drop_index('ix_portfolio_members_source_id', table_name='portfolio_members')
    op.drop_index('ix_portfolio_members_source_type', table_name='portfolio_members')
    op.drop_index('ix_portfolio_members_status', table_name='portfolio_members')
    op.drop_index('ix_portfolio_members_symbol_id', table_name='portfolio_members')
    op.drop_index('ix_portfolio_members_portfolio_id', table_name='portfolio_members')
    op.drop_table('portfolio_members')
