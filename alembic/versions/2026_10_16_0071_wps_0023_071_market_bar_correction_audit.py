"""Permit explicit, atomic market-bar correction receipts without losing evidence.

Revision ID: wps_0023_071_market_bar_correction_audit
Revises: wps_0023_070_signal_rules_contract

Revision 068 only added absent named constraints. An existing audit CHECK may
therefore still omit the four factor-governance actions. Replace that CHECK
with the complete previous 17 actions plus MARKET_BAR_CORRECTION. SQLite must
rebuild the table; reflection retains its rows, indexes, and foreign keys.

MySQL 5.7 parses but does not enforce CHECK constraints. Its runtime action
validation remains necessary; this revision does not claim to change that.

Downgrade intentionally keeps the widened constraint, as revisions 038/043 do:
tightening it would reject or require rewriting already-persisted receipts.
No audit row is rewritten, deleted, or disguised as UNKNOWN_AUDIT_ACTION.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "wps_0023_071_market_bar_correction_audit"
down_revision = "wps_0023_070_signal_rules_contract"
branch_labels = None
depends_on = None

_TABLE = "data_governance_audit_events"
_CONSTRAINT = "ck_dg_audit_action_values"
# Migration history is self-contained; never import the mutable runtime enum.
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
    "FACTOR_DRAFT_SUBMITTED",
    "FACTOR_DRAFT_APPROVED",
    "FACTOR_DRAFT_REJECTED",
    "FACTOR_VERSION_PROMOTED",
    "MARKET_BAR_CORRECTION",
    "UNKNOWN_AUDIT_ACTION",
)
_CHECK = "action IN ({})".format(
    ",".join("'{}'".format(action) for action in _ACTIONS)
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table(_TABLE):
        raise RuntimeError("data_governance_audit_events is missing: apply earlier revisions first")
    existing = next(
        (item for item in inspector.get_check_constraints(_TABLE)
         if item.get("name") == _CONSTRAINT),
        None,
    )
    if existing and all("'{}'".format(action) in str(existing.get("sqltext") or "")
                        for action in _ACTIONS):
        return

    # Fail before DDL if this installation contains an unrecognized extension.
    # Never rewrite historical evidence to make the replacement CHECK fit.
    table = sa.table(_TABLE, sa.column("action", sa.String(64)))
    unknown = list(connection.scalars(
        sa.select(table.c.action).where(table.c.action.not_in(_ACTIONS)).distinct()
    ))
    if unknown:
        raise RuntimeError("Unrecognized persisted audit actions; review before replacing CHECK")

    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            if existing:
                batch.drop_constraint(_CONSTRAINT, type_="check")
            batch.create_check_constraint(_CONSTRAINT, _CHECK)
    else:
        if existing:
            op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
        op.create_check_constraint(_CONSTRAINT, _TABLE, _CHECK)


def downgrade() -> None:
    # Existing correction receipts and factor-approval evidence must stay valid.
    pass
