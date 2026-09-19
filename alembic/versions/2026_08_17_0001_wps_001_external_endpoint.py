"""wps_001_external_endpoint.

Revision ID: wps_001_external_endpoint
Revises: 
Create Date: 2026-08-17 00:01:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_001_external_endpoint'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'external_endpoint_runtime',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('interface_key', String(128), nullable=False, unique=True),
        Column('host', String(128), nullable=True),
        Column('state', String(16), nullable=False, server_default='closed'),
        Column('consecutive_failures', Integer, nullable=False, server_default='0'),
        Column('cooldown_until', DateTime, nullable=True),
        Column('last_error_code', String(64), nullable=True),
        Column('last_error_at', DateTime, nullable=True),
        Column('request_count', Integer, nullable=False, server_default='0'),
        Column('cache_hit_count', Integer, nullable=False, server_default='0'),
        Column('fallback_count', Integer, nullable=False, server_default='0'),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_external_endpoint_runtime_interface_key', 'external_endpoint_runtime', ['interface_key'], unique=True)
    op.create_index('ix_external_endpoint_runtime_host', 'external_endpoint_runtime', ['host'])
    op.create_index('ix_external_endpoint_runtime_state', 'external_endpoint_runtime', ['state'])


def downgrade() -> None:
    op.drop_index('ix_external_endpoint_runtime_state', table_name='external_endpoint_runtime')
    op.drop_index('ix_external_endpoint_runtime_host', table_name='external_endpoint_runtime')
    op.drop_index('ix_external_endpoint_runtime_interface_key', table_name='external_endpoint_runtime')
    op.drop_table('external_endpoint_runtime')
