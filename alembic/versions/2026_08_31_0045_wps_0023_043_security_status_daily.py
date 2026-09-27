"""SecurityStatusDaily ORM + Alembic 迁移。

Revision ID: wps_0023_043_security_status_daily
Revises: wps_0023_042_decision_order_plans
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_043_security_status_daily"
down_revision = "wps_0023_042_decision_order_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "security_status_daily" in inspector.get_table_names():
        return
    op.create_table(
        "security_status_daily",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "symbol_id",
            sa.Integer,
            sa.ForeignKey("symbols.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("is_st", sa.Boolean, nullable=False),
        sa.Column("is_suspended", sa.Boolean, nullable=False),
        sa.Column("is_delisting_period", sa.Boolean, nullable=False),
        sa.Column("is_listed", sa.Boolean, nullable=False),
        sa.Column("listing_date", sa.Date, nullable=True),
        sa.Column("delisting_date", sa.Date, nullable=True),
        sa.Column("status_source", sa.String(64), nullable=False),
        sa.Column("source_updated_at", sa.DateTime, nullable=True),
        sa.Column("as_of_batch_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_security_status_daily_symbol_date",
        "security_status_daily",
        ["symbol_id", "trade_date"],
        unique=True,
    )
    op.create_index(
        "ix_security_status_daily_trade_date",
        "security_status_daily",
        ["trade_date"],
        unique=False,
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "security_status_daily" not in inspector.get_table_names():
        return
    for item in inspector.get_indexes("security_status_daily"):
        if item.get("name"):
            op.drop_index(item["name"], table_name="security_status_daily")
    op.drop_table("security_status_daily")
