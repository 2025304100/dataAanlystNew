"""Add explicit audit actions for G6 rollout lifecycle.

Revision ID: wps_0023_041_g6_rollout_audit_actions
Revises: wps_0023_040_g5_dual_run_reports
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_041_g6_rollout_audit_actions"
down_revision = "wps_0023_040_g5_dual_run_reports"
branch_labels = None
depends_on = None

_ACTIONS = (
    "PORTFOLIO_CANDIDATE_SCD2_CHANGE",
    "BENCHMARK_SOURCE_FAILOVER",
    "AUTO_SIMULATION_RESULT",
    "RECONCILIATION_RESULT",
    "ILLEGAL_STATE_TRANSITION",
    "FACTOR_USAGE_APPLIED",
    "OUTBOX_EVENT_DISPATCHED",
    "DATA_BLOCK_RESOLUTION",
    "DATA_SOURCE_FAILOVER",
    "DATA_QUALITY_QUARANTINE",
    "G6_ROLLOUT_STARTED",
    "G6_ROLLOUT_ROLLED_BACK",
    "UNKNOWN_AUDIT_ACTION",
)
_CHECK = "action IN ({})".format(",".join("'{}'".format(v) for v in _ACTIONS))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "data_governance_audit_events" not in inspector.get_table_names():
        return
    checks = inspector.get_check_constraints("data_governance_audit_events")
    named = next(
        (item.get("name") for item in checks if item.get("name") == "ck_dg_audit_action_values"),
        None,
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("data_governance_audit_events", recreate="always") as batch:
            if named:
                batch.drop_constraint(named, type_="check")
            batch.create_check_constraint("ck_dg_audit_action_values", _CHECK)
    else:
        if named:
            op.drop_constraint(named, "data_governance_audit_events", type_="check")
        op.create_check_constraint(
            "ck_dg_audit_action_values", "data_governance_audit_events", _CHECK,
        )


def downgrade() -> None:
    # Keep the forward-compatible constraint on downgrade so existing G6
    # evidence remains readable by database tooling.
    pass
