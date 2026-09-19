"""wps_0023_016_portfolio_reviews.

Revision ID: wps_0023_016_portfolio_reviews
Revises: wps_0023_015_ai_session_tables
Create Date: 2026-08-17 00:17:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_016_portfolio_reviews'
down_revision = 'wps_0023_015_ai_session_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'journal_entries',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, nullable=True),
        Column('symbol_id', Integer, nullable=True),
        Column('entry_type', String(32), nullable=False),
        Column('title', String(256), nullable=False),
        Column('content', Text, nullable=True),
        Column('tags_json', Text, nullable=True),
        Column('attachments_json', Text, nullable=True),
        Column('trade_date', String(32), nullable=True),
        Column('created_by', String(128), nullable=False, server_default='local_user'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_journal_entries_portfolio_id', 'journal_entries', ['portfolio_id'])
    op.create_index('ix_journal_entries_symbol_id', 'journal_entries', ['symbol_id'])
    op.create_index('ix_journal_entries_entry_type', 'journal_entries', ['entry_type'])
    op.create_index('ix_journal_entries_trade_date', 'journal_entries', ['trade_date'])
    op.create_index('ix_journal_entries_created_at', 'journal_entries', ['created_at'])

    op.create_table(
        'reviews',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('review_type', String(32), nullable=False),
        Column('target_type', String(32), nullable=False),
        Column('target_id', Integer, nullable=False),
        Column('title', String(256), nullable=False),
        Column('content', Text, nullable=True),
        Column('rating', Integer, nullable=True),
        Column('status', String(16), nullable=False, server_default='pending'),
        Column('reviewed_by', String(128), nullable=True),
        Column('reviewed_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_reviews_review_type', 'reviews', ['review_type'])
    op.create_index('ix_reviews_target_type_target_id', 'reviews', ['target_type', 'target_id'])
    op.create_index('ix_reviews_status', 'reviews', ['status'])
    op.create_index('ix_reviews_created_at', 'reviews', ['created_at'])

    op.create_table(
        'ai_deprecation_logs',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('profile_id', Integer, nullable=True),
        Column('deprecation_type', String(32), nullable=False),
        Column('old_value', Text, nullable=True),
        Column('new_value', Text, nullable=True),
        Column('reason', Text, nullable=True),
        Column('migration_status', String(16), nullable=False, server_default='pending'),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_ai_deprecation_logs_profile_id', 'ai_deprecation_logs', ['profile_id'])
    op.create_index('ix_ai_deprecation_logs_deprecation_type', 'ai_deprecation_logs', ['deprecation_type'])
    op.create_index('ix_ai_deprecation_logs_migration_status', 'ai_deprecation_logs', ['migration_status'])
    op.create_index('ix_ai_deprecation_logs_created_at', 'ai_deprecation_logs', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_ai_deprecation_logs_created_at', table_name='ai_deprecation_logs')
    op.drop_index('ix_ai_deprecation_logs_migration_status', table_name='ai_deprecation_logs')
    op.drop_index('ix_ai_deprecation_logs_deprecation_type', table_name='ai_deprecation_logs')
    op.drop_index('ix_ai_deprecation_logs_profile_id', table_name='ai_deprecation_logs')
    op.drop_table('ai_deprecation_logs')

    op.drop_index('ix_reviews_created_at', table_name='reviews')
    op.drop_index('ix_reviews_status', table_name='reviews')
    op.drop_index('ix_reviews_target_type_target_id', table_name='reviews')
    op.drop_index('ix_reviews_review_type', table_name='reviews')
    op.drop_table('reviews')

    op.drop_index('ix_journal_entries_created_at', table_name='journal_entries')
    op.drop_index('ix_journal_entries_trade_date', table_name='journal_entries')
    op.drop_index('ix_journal_entries_entry_type', table_name='journal_entries')
    op.drop_index('ix_journal_entries_symbol_id', table_name='journal_entries')
    op.drop_index('ix_journal_entries_portfolio_id', table_name='journal_entries')
    op.drop_table('journal_entries')
