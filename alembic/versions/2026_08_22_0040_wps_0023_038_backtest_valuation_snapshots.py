"""Persist immutable completed-run valuation marks.

Revision ID: wps_0023_038_backtest_valuation_snapshots
Revises: wps_0023_037_backtest_execution_fills
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_038_backtest_valuation_snapshots"
down_revision = "wps_0023_037_backtest_execution_fills"
branch_labels = None
depends_on = None


TABLE = "backtest_valuation_snapshots"


def _table_exists(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _table_exists(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("symbol_id", sa.Integer, sa.ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mark_price", sa.Float, nullable=True),
        sa.Column("market_value", sa.Float, nullable=True),
        sa.Column("portfolio_equity", sa.Float, nullable=True),
        sa.Column("weight", sa.Float, nullable=True),
        sa.Column("price_bar_id", sa.Integer, nullable=True),
        sa.Column("price_source", sa.String(32), nullable=True),
        sa.Column("price_available_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_backtest_valuation_snapshots_run_id", TABLE, ["run_id"])
    op.create_index("ix_backtest_valuation_snapshots_trade_date", TABLE, ["trade_date"])
    op.create_index("ix_backtest_valuation_snapshots_symbol_id", TABLE, ["symbol_id"])
    op.create_index(
        "uq_backtest_valuation_snapshot_run_date_symbol",
        TABLE,
        ["run_id", "trade_date", "symbol_id"],
        unique=True,
    )


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    op.drop_index("uq_backtest_valuation_snapshot_run_date_symbol", table_name=TABLE)
    op.drop_index("ix_backtest_valuation_snapshots_symbol_id", table_name=TABLE)
    op.drop_index("ix_backtest_valuation_snapshots_trade_date", table_name=TABLE)
    op.drop_index("ix_backtest_valuation_snapshots_run_id", table_name=TABLE)
    op.drop_table(TABLE)
