"""WP8: portfolio_reviews table

Revision ID: wps_0023_016_portfolio_reviews
Revises: wps_0023_015_ai_session_tables
Create Date: 2026-07-23 00:16:00.000000

创建 portfolio_reviews 表（WP8 组合复盘记录）。
init_db.py SQLite/MySQL 路径都补了，Alembic revision 缺失。
幂等：表已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_016_portfolio_reviews"
down_revision: Union[str, None] = "wps_0023_015_ai_session_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "portfolio_reviews"):
        op.create_table(
            "portfolio_reviews",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("portfolio_id", sa.Integer(), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("report_snapshot_json", sa.Text(), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_portfolio_reviews_portfolio_id", "portfolio_reviews", ["portfolio_id"], unique=False)
        op.create_index("ix_portfolio_reviews_created_at", "portfolio_reviews", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "portfolio_reviews"):
        op.drop_table("portfolio_reviews")
