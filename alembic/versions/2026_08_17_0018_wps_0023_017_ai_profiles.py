"""wps_0023_017_ai_profiles.

Revision ID: wps_0023_017_ai_profiles
Revises: wps_0023_016_portfolio_reviews
Create Date: 2026-08-17 00:18:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_017_ai_profiles'
down_revision = 'wps_0023_016_portfolio_reviews'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_profiles',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(64), nullable=False, unique=True),
        Column('provider', String(64), nullable=False),
        Column('base_url', String(256), nullable=True),
        Column('model', String(128), nullable=False),
        Column('auth_type', String(32), nullable=True),
        Column('secret_key_ref', String(128), nullable=True),
        Column('timeout_seconds', Integer, nullable=False, server_default='30'),
        Column('max_tokens', Integer, nullable=False, server_default='4096'),
        Column('max_context_tokens', Integer, nullable=False, server_default='8192'),
        Column('daily_request_limit', Integer, nullable=False, server_default='100'),
        Column('max_concurrent', Integer, nullable=False, server_default='3'),
        Column('purpose', String(64), nullable=False, server_default='all'),
        Column('priority', Integer, nullable=False, server_default='0'),
        Column('is_enabled', Integer, nullable=False, server_default='1'),
        Column('is_fallback', Integer, nullable=False, server_default='0'),
        Column('health_status', String(16), nullable=False, server_default='unknown'),
        Column('last_health_check', DateTime, nullable=True),
        Column('daily_request_count', Integer, nullable=False, server_default='0'),
        Column('daily_request_reset_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=True),
    )
    op.create_index('ix_ai_profiles_name', 'ai_profiles', ['name'], unique=True)
    op.create_index('ix_ai_profiles_provider', 'ai_profiles', ['provider'])
    op.create_index('ix_ai_profiles_purpose', 'ai_profiles', ['purpose'])
    op.create_index('ix_ai_profiles_priority', 'ai_profiles', ['priority'])
    op.create_index('ix_ai_profiles_is_enabled', 'ai_profiles', ['is_enabled'])
    op.create_index('ix_ai_profiles_health_status', 'ai_profiles', ['health_status'])


def downgrade() -> None:
    op.drop_index('ix_ai_profiles_health_status', table_name='ai_profiles')
    op.drop_index('ix_ai_profiles_is_enabled', table_name='ai_profiles')
    op.drop_index('ix_ai_profiles_priority', table_name='ai_profiles')
    op.drop_index('ix_ai_profiles_purpose', table_name='ai_profiles')
    op.drop_index('ix_ai_profiles_provider', table_name='ai_profiles')
    op.drop_index('ix_ai_profiles_name', table_name='ai_profiles')
    op.drop_table('ai_profiles')
