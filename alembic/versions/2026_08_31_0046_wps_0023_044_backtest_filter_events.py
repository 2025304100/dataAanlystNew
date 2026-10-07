"""BacktestFilterEvent ORM + Alembic 迁移。

Revision ID: wps_0023_044_backtest_filter_events
Revises: wps_0023_043_security_status_daily
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_044_backtest_filter_events"
down_revision = "wps_0023_043_security_status_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "backtest_filter_events" in inspector.get_table_names():
        return
    op.create_table(
        "backtest_filter_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Integer,
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column(
            "symbol_id",
            sa.Integer,
            sa.ForeignKey("symbols.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("rule_code", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("raw_status_json", sa.Text, nullable=False),
        sa.Column("effective_status", sa.String(32), nullable=False, server_default="LISTED"),
        sa.Column("price_used", sa.Float, nullable=True),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("data_batch_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_bfe_run_date",
        "backtest_filter_events",
        ["run_id", "trade_date"],
        unique=False,
    )
    op.create_index(
        "ix_bfe_symbol_date",
        "backtest_filter_events",
        ["symbol_id", "trade_date"],
        unique=False,
    )
    op.create_index(
        "ix_bfe_rule_code",
        "backtest_filter_events",
        ["rule_code"],
        unique=False,
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "backtest_filter_events" not in inspector.get_table_names():
        return
    for item in inspector.get_indexes("backtest_filter_events"):
        if item.get("name"):
            op.drop_index(item["name"], table_name="backtest_filter_events")
    op.drop_table("backtest_filter_events")
