"""Persist immutable execution events for decision-driven backtests.

``backtest_trades`` is deliberately retained as the aggregate lot and PnL
view.  This table records each successful matcher fill, including partial
retries, so a historical holding ledger can be reconstructed without assigning
later fills to the first lot-entry date.

Revision ID: wps_0023_037_backtest_execution_fills
Revises: wps_0023_036_data_quality_snapshot_contract
Create Date: 2026-08-22 09:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_037_backtest_execution_fills"
down_revision = "wps_0023_036_data_quality_snapshot_contract"
branch_labels = None
depends_on = None


TABLE = "backtest_execution_fills"


def _table_exists(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _table_exists(TABLE):
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Integer,
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "backtest_trade_id",
            sa.Integer,
            sa.ForeignKey("backtest_trades.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "symbol_id",
            sa.Integer,
            sa.ForeignKey("symbols.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("execution_date", sa.Date, nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("executed_price", sa.Float, nullable=False),
        sa.Column("cost", sa.Float, nullable=False, server_default="0"),
        sa.Column(
            "decision_evidence_id",
            sa.String(64),
            sa.ForeignKey("decision_evidence.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("order_plan_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    for name, columns in (
        ("ix_backtest_execution_fills_run_id", ["run_id"]),
        ("ix_backtest_execution_fills_backtest_trade_id", ["backtest_trade_id"]),
        ("ix_backtest_execution_fills_symbol_id", ["symbol_id"]),
        ("ix_backtest_execution_fills_execution_date", ["execution_date"]),
        ("ix_backtest_execution_fills_side", ["side"]),
        ("ix_backtest_execution_fills_decision_evidence_id", ["decision_evidence_id"]),
        ("ix_backtest_execution_fills_order_plan_id", ["order_plan_id"]),
        (
            "ix_backtest_execution_fills_run_date_symbol",
            ["run_id", "execution_date", "symbol_id"],
        ),
    ):
        op.create_index(name, TABLE, columns, unique=False)


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    inspector = sa.inspect(op.get_bind())
    for index in inspector.get_indexes(TABLE):
        name = index.get("name")
        if name:
            op.drop_index(name, table_name=TABLE)
    op.drop_table(TABLE)
