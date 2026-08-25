"""wps_0023_009_factor_models.

Revision ID: wps_0023_009_factor_models
Revises: wps_0023_008_factors
Create Date: 2026-08-17 00:10:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_009_factor_models'
down_revision = 'wps_0023_008_factors'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'macro_data',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('indicator_code', String(64), nullable=False),
        Column('indicator_name', String(128), nullable=False),
        Column('period', String(16), nullable=False),
        Column('value', String(64), nullable=True),
        Column('data_date', String(32), nullable=False),
        Column('source', String(32), nullable=False, server_default='akshare'),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_macro_data_indicator_code', 'macro_data', ['indicator_code'])
    op.create_index('ix_macro_data_data_date', 'macro_data', ['data_date'])

    op.create_table(
        'news_events',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('title', String(256), nullable=False),
        Column('content', Text, nullable=True),
        Column('symbol_id', Integer, nullable=True),
        Column('news_type', String(32), nullable=True),
        Column('source', String(64), nullable=True),
        Column('published_at', DateTime, nullable=True),
        Column('url', String(512), nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_news_events_symbol_id', 'news_events', ['symbol_id'])
    op.create_index('ix_news_events_news_type', 'news_events', ['news_type'])
    op.create_index('ix_news_events_published_at', 'news_events', ['published_at'])

    op.create_table(
        'custom_indicators',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(128), nullable=False),
        Column('indicator_type', String(32), nullable=False),
        Column('formula_expr', Text, nullable=False),
        Column('params_json', Text, nullable=True),
        Column('description', Text, nullable=True),
        Column('is_public', Integer, nullable=False, server_default='0'),
        Column('owner_id', String(64), nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_custom_indicators_indicator_type', 'custom_indicators', ['indicator_type'])
    op.create_index('ix_custom_indicators_is_public', 'custom_indicators', ['is_public'])

    op.create_table(
        'ai_drafts',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('draft_type', String(32), nullable=False),
        Column('title', String(256), nullable=False),
        Column('content_json', Text, nullable=False),
        Column('status', String(16), nullable=False, server_default='draft'),
        Column('session_id', Integer, nullable=True),
        Column('message_id', Integer, nullable=True),
        Column('created_by', String(128), nullable=False, server_default='local_user'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_ai_drafts_draft_type', 'ai_drafts', ['draft_type'])
    op.create_index('ix_ai_drafts_status', 'ai_drafts', ['status'])
    op.create_index('ix_ai_drafts_session_id', 'ai_drafts', ['session_id'])


def downgrade() -> None:
    op.drop_index('ix_ai_drafts_session_id', table_name='ai_drafts')
    op.drop_index('ix_ai_drafts_status', table_name='ai_drafts')
    op.drop_index('ix_ai_drafts_draft_type', table_name='ai_drafts')
    op.drop_table('ai_drafts')

    op.drop_index('ix_custom_indicators_is_public', table_name='custom_indicators')
    op.drop_index('ix_custom_indicators_indicator_type', table_name='custom_indicators')
    op.drop_table('custom_indicators')

    op.drop_index('ix_news_events_published_at', table_name='news_events')
    op.drop_index('ix_news_events_news_type', table_name='news_events')
    op.drop_index('ix_news_events_symbol_id', table_name='news_events')
    op.drop_table('news_events')

    op.drop_index('ix_macro_data_data_date', table_name='macro_data')
    op.drop_index('ix_macro_data_indicator_code', table_name='macro_data')
    op.drop_table('macro_data')
