"""BFG: extend backtest_runs / factor_evaluation_runs / backtest_trades / backtest_execution_fills with filter governance fields.

Revision ID: wps_0023_045_bfg_extend_run_fields
Revises: wps_0023_044_backtest_filter_events
Create Date: 2026-08-31 00:47:00
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "wps_0023_045_bfg_extend_run_fields"
down_revision: str | None = "wps_0023_044_backtest_filter_events"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    """Return True when the column already exists in the target table.

    All ALTER operations are guarded by this check so the migration remains
    idempotent across environments where some columns may already have been
    added by a manual patch.
    """
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns(table_name)}
    return column_name in existing


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1) backtest_runs: filter governance snapshot + fidelity markers
    # ------------------------------------------------------------------
    if not _column_exists("backtest_runs", "filter_config_json"):
        op.add_column(
            "backtest_runs",
            sa.Column("filter_config_json", sa.Text(), nullable=True),
        )
    if not _column_exists("backtest_runs", "config_hash"):
        op.add_column(
            "backtest_runs",
            sa.Column("config_hash", sa.String(length=64), nullable=True),
        )
    if not _column_exists("backtest_runs", "production_fidelity"):
        op.add_column(
            "backtest_runs",
            sa.Column(
                "production_fidelity",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
    if not _column_exists("backtest_runs", "non_fidelity_reason"):
        op.add_column(
            "backtest_runs",
            sa.Column("non_fidelity_reason", sa.Text(), nullable=True),
        )

    # ------------------------------------------------------------------
    # 2) factor_evaluation_runs: filter snapshot + fidelity + 3-count
    # ------------------------------------------------------------------
    if not _column_exists("factor_evaluation_runs", "filter_config_json"):
        op.add_column(
            "factor_evaluation_runs",
            sa.Column("filter_config_json", sa.Text(), nullable=True),
        )
    if not _column_exists("factor_evaluation_runs", "config_hash"):
        op.add_column(
            "factor_evaluation_runs",
            sa.Column("config_hash", sa.String(length=64), nullable=True),
        )
    if not _column_exists("factor_evaluation_runs", "production_fidelity"):
        op.add_column(
            "factor_evaluation_runs",
            sa.Column(
                "production_fidelity",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
    if not _column_exists("factor_evaluation_runs", "non_fidelity_reason"):
        op.add_column(
            "factor_evaluation_runs",
            sa.Column("non_fidelity_reason", sa.Text(), nullable=True),
        )
    if not _column_exists("factor_evaluation_runs", "raw_count"):
        op.add_column(
            "factor_evaluation_runs",
            sa.Column("raw_count", sa.Integer(), nullable=True),
        )
    if not _column_exists("factor_evaluation_runs", "filtered_count"):
        op.add_column(
            "factor_evaluation_runs",
            sa.Column("filtered_count", sa.Integer(), nullable=True),
        )
    if not _column_exists("factor_evaluation_runs", "effective_count"):
        op.add_column(
            "factor_evaluation_runs",
            sa.Column("effective_count", sa.Integer(), nullable=True),
        )

    # ------------------------------------------------------------------
    # 3) backtest_trades: config_hash join marker
    # ------------------------------------------------------------------
    if not _column_exists("backtest_trades", "config_hash"):
        op.add_column(
            "backtest_trades",
            sa.Column("config_hash", sa.String(length=64), nullable=True),
        )

    # ------------------------------------------------------------------
    # 4) backtest_execution_fills: config_hash join marker
    # ------------------------------------------------------------------
    if not _column_exists("backtest_execution_fills", "config_hash"):
        op.add_column(
            "backtest_execution_fills",
            sa.Column("config_hash", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # 4) backtest_execution_fills
    # ------------------------------------------------------------------
    if _column_exists("backtest_execution_fills", "config_hash"):
        op.drop_column("backtest_execution_fills", "config_hash")

    # ------------------------------------------------------------------
    # 3) backtest_trades
    # ------------------------------------------------------------------
    if _column_exists("backtest_trades", "config_hash"):
        op.drop_column("backtest_trades", "config_hash")

    # ------------------------------------------------------------------
    # 2) factor_evaluation_runs
    # ------------------------------------------------------------------
    if _column_exists("factor_evaluation_runs", "effective_count"):
        op.drop_column("factor_evaluation_runs", "effective_count")
    if _column_exists("factor_evaluation_runs", "filtered_count"):
        op.drop_column("factor_evaluation_runs", "filtered_count")
    if _column_exists("factor_evaluation_runs", "raw_count"):
        op.drop_column("factor_evaluation_runs", "raw_count")
    if _column_exists("factor_evaluation_runs", "non_fidelity_reason"):
        op.drop_column("factor_evaluation_runs", "non_fidelity_reason")
    if _column_exists("factor_evaluation_runs", "production_fidelity"):
        op.drop_column("factor_evaluation_runs", "production_fidelity")
    if _column_exists("factor_evaluation_runs", "config_hash"):
        op.drop_column("factor_evaluation_runs", "config_hash")
    if _column_exists("factor_evaluation_runs", "filter_config_json"):
        op.drop_column("factor_evaluation_runs", "filter_config_json")

    # ------------------------------------------------------------------
    # 1) backtest_runs
    # ------------------------------------------------------------------
    if _column_exists("backtest_runs", "non_fidelity_reason"):
        op.drop_column("backtest_runs", "non_fidelity_reason")
    if _column_exists("backtest_runs", "production_fidelity"):
        op.drop_column("backtest_runs", "production_fidelity")
    if _column_exists("backtest_runs", "config_hash"):
        op.drop_column("backtest_runs", "config_hash")
    if _column_exists("backtest_runs", "filter_config_json"):
        op.drop_column("backtest_runs", "filter_config_json")
