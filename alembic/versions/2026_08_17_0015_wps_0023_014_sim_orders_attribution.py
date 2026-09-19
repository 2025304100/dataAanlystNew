"""wps_0023_014_sim_orders_attribution.

Revision ID: wps_0023_014_sim_orders_attribution
Revises: wps_0023_013_notification_tables
Create Date: 2026-08-17 00:15:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_014_sim_orders_attribution'
down_revision = 'wps_0023_013_notification_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sim_orders_attribution',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('sim_order_id', Integer, nullable=False),
        Column('portfolio_member_id', Integer, nullable=True),
        Column('attribution_type', String(32), nullable=False),
        Column('attribution_id', Integer, nullable=True),
        Column('attribution_snapshot_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_sim_orders_attribution_sim_order_id', 'sim_orders_attribution', ['sim_order_id'])
    op.create_index('ix_sim_orders_attribution_portfolio_member_id', 'sim_orders_attribution', ['portfolio_member_id'])
    op.create_index('ix_sim_orders_attribution_attribution_type', 'sim_orders_attribution', ['attribution_type'])

    op.create_table(
        'portfolio_reviews',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, nullable=False),
        Column('review_type', String(32), nullable=False),
        Column('review_date', String(32), nullable=False),
        Column('title', String(256), nullable=False),
        Column('content', Text, nullable=True),
        Column('metrics_json', Text, nullable=True),
        Column('action_items_json', Text, nullable=True),
        Column('status', String(16), nullable=False, server_default='draft'),
        Column('reviewed_by', String(128), nullable=True),
        Column('reviewed_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_portfolio_reviews_portfolio_id', 'portfolio_reviews', ['portfolio_id'])
    op.create_index('ix_portfolio_reviews_review_type', 'portfolio_reviews', ['review_type'])
    op.create_index('ix_portfolio_reviews_review_date', 'portfolio_reviews', ['review_date'])
    op.create_index('ix_portfolio_reviews_status', 'portfolio_reviews', ['status'])


def downgrade() -> None:
    op.drop_index('ix_portfolio_reviews_status', table_name='portfolio_reviews')
    op.drop_index('ix_portfolio_reviews_review_date', table_name='portfolio_reviews')
    op.drop_index('ix_portfolio_reviews_review_type', table_name='portfolio_reviews')
    op.drop_index('ix_portfolio_reviews_portfolio_id', table_name='portfolio_reviews')
    op.drop_table('portfolio_reviews')

    op.drop_index('ix_sim_orders_attribution_attribution_type', table_name='sim_orders_attribution')
    op.drop_index('ix_sim_orders_attribution_portfolio_member_id', table_name='sim_orders_attribution')
    op.drop_index('ix_sim_orders_attribution_sim_order_id', table_name='sim_orders_attribution')
    op.drop_table('sim_orders_attribution')
