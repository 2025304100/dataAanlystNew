"""Mark historical backtests that cannot be replayed from immutable inputs.

Revision ID: wps_0023_039_backtest_reproducibility_marker
Revises: wps_0023_038_backtest_valuation_snapshots
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_039_backtest_reproducibility_marker"
down_revision = "wps_0023_038_backtest_valuation_snapshots"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("backtest_runs")}


def upgrade() -> None:
    columns = _columns()
    if "reproducibility_status" not in columns:
        op.add_column(
            "backtest_runs",
            sa.Column(
                "reproducibility_status",
                sa.String(32),
                nullable=False,
                server_default="legacy/non_reproducible",
            ),
        )
    if "reproducibility_reason" not in columns:
        op.add_column("backtest_runs", sa.Column("reproducibility_reason", sa.Text(), nullable=True))

    bind = op.get_bind()
    # Do not infer reproducibility from status alone: an old completed run can
    # have a strategy id copied by a partial migration but still lack evidence.
    bind.execute(sa.text(
        "UPDATE backtest_runs SET reproducibility_status = "
        "CASE WHEN strategy_snapshot_id IS NOT NULL "
        "AND decision_run_ids_json IS NOT NULL "
        "AND decision_run_ids_json NOT IN ('', '[]') "
        "THEN 'reproducible' ELSE 'legacy/non_reproducible' END"
    ))
    bind.execute(sa.text(
        "UPDATE backtest_runs SET reproducibility_reason = "
        "'历史运行未绑定策略执行快照和统一决策链' "
        "WHERE reproducibility_status = 'legacy/non_reproducible' "
        "AND (reproducibility_reason IS NULL OR reproducibility_reason = '')"
    ))


def downgrade() -> None:
    columns = _columns()
    if "reproducibility_reason" in columns:
        op.drop_column("backtest_runs", "reproducibility_reason")
    if "reproducibility_status" in columns:
        op.drop_column("backtest_runs", "reproducibility_status")
