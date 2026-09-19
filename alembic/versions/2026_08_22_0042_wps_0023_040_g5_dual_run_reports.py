"""Persist auditable G5 new/old dual-run summaries.

Revision ID: wps_0023_040_g5_dual_run_reports
Revises: wps_0023_039_backtest_reproducibility_marker
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_040_g5_dual_run_reports"
down_revision = "wps_0023_039_backtest_reproducibility_marker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "g5_dual_run_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("portfolio_id", sa.Integer(), sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("total_days", sa.Integer(), nullable=False),
        sa.Column("days_replayed", sa.Integer(), nullable=False),
        sa.Column("skipped_day_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("p0_unexplained_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("p1_hold_noaction_flip_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible_for_g6", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("report_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_g5_dual_run_reports_portfolio_created", "g5_dual_run_reports", ["portfolio_id", "created_at"])
    op.create_index("ix_g5_dual_run_reports_portfolio_id", "g5_dual_run_reports", ["portfolio_id"])
    op.create_index("ix_g5_dual_run_reports_start_date", "g5_dual_run_reports", ["start_date"])
    op.create_index("ix_g5_dual_run_reports_end_date", "g5_dual_run_reports", ["end_date"])
    op.create_index("ix_g5_dual_run_reports_eligible_for_g6", "g5_dual_run_reports", ["eligible_for_g6"])
    op.create_index("ix_g5_dual_run_reports_report_hash", "g5_dual_run_reports", ["report_hash"])


def downgrade() -> None:
    op.drop_table("g5_dual_run_reports")
