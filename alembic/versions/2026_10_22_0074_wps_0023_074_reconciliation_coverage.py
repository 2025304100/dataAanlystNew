"""Persist immutable account baselines and generation-scoped reconciliation.

This is additive storage only. No ledger rows, legacy checkpoints, runtime
states, or historical reports are rewritten or promoted by this migration.
"""
from alembic import op
import sqlalchemy as sa


revision = "wps_0023_074_reconciliation_coverage"
down_revision = "wps_0023_073_discovery_snapshot_versions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portfolio_account_baselines",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), sa.ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("as_of_at", sa.DateTime(), nullable=False),
        sa.Column("coverage_start_trade_date", sa.Date(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_refs_json", sa.Text(), nullable=False),
        sa.Column("history_gaps_json", sa.Text(), nullable=False),
        sa.Column("accepted_audit_id", sa.Integer(), sa.ForeignKey("data_governance_audit_events.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("supersedes_id", sa.String(64), sa.ForeignKey("portfolio_account_baselines.id", ondelete="RESTRICT")),
        sa.Column("acceptance_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("portfolio_id", "acceptance_key", name="uq_pab_portfolio_acceptance"),
        sa.CheckConstraint("kind IN ('initial','rebaseline')", name="ck_pab_kind"),
    )
    op.create_index("ix_pab_portfolio_scope_time", "portfolio_account_baselines", ["portfolio_id", "scope_key", "created_at"])
    op.create_table(
        "portfolio_reconciliation_cursors",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), sa.ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("baseline_id", sa.String(64), sa.ForeignKey("portfolio_account_baselines.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("coverage_start_trade_date", sa.Date(), nullable=False),
        sa.Column("verified_through_trade_date", sa.Date()),
        sa.Column("calendar_version", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("portfolio_id", "scope_key", name="uq_prc_portfolio_scope"),
        sa.CheckConstraint("generation >= 1 AND row_version >= 1", name="ck_prc_versions"),
    )
    op.create_table(
        "portfolio_reconciliation_days",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("cursor_id", sa.String(64), sa.ForeignKey("portfolio_reconciliation_cursors.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("target_trade_date", sa.Date(), nullable=False),
        sa.Column("due_at", sa.DateTime()), sa.Column("next_check_at", sa.DateTime()),
        sa.Column("work_state", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(64), nullable=False),
        sa.Column("closure_type", sa.String(64)),
        sa.Column("requested_evidence_version", sa.Integer(), nullable=False),
        sa.Column("processed_evidence_version", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("current_report_id", sa.String(64)), sa.Column("task_id", sa.String(64)),
        sa.Column("last_error_code", sa.String(64)), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("cursor_id", "generation", "target_trade_date", name="uq_prd_cursor_generation_day"),
        sa.CheckConstraint("work_state IN ('queued','running','waiting_execution','waiting_data','needs_review','done','cancelled')", name="ck_prd_work_state"),
        sa.CheckConstraint("generation >= 1 AND requested_evidence_version >= 1 AND processed_evidence_version >= 0", name="ck_prd_versions"),
    )
    op.create_index("ix_prd_work_next_check", "portfolio_reconciliation_days", ["work_state", "next_check_at"])
    op.create_table(
        "portfolio_reconciliation_reports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("day_id", sa.String(64), sa.ForeignKey("portfolio_reconciliation_days.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("report_version", sa.Integer(), nullable=False), sa.Column("evidence_version", sa.Integer(), nullable=False),
        sa.Column("baseline_id", sa.String(64), sa.ForeignKey("portfolio_account_baselines.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False), sa.Column("findings_json", sa.Text(), nullable=False),
        sa.Column("source_refs_json", sa.Text(), nullable=False), sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(64), nullable=False), sa.Column("closure_type", sa.String(64)),
        sa.Column("policy_version", sa.String(64), nullable=False), sa.Column("calendar_version", sa.String(64), nullable=False),
        sa.Column("audit_event_id", sa.Integer(), sa.ForeignKey("data_governance_audit_events.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("day_id", "report_version", name="uq_prr_day_version"),
        sa.CheckConstraint("report_version >= 1 AND evidence_version >= 1", name="ck_prr_versions"),
    )


def downgrade():
    connection = op.get_bind()
    for table in ("portfolio_account_baselines", "portfolio_reconciliation_cursors", "portfolio_reconciliation_days", "portfolio_reconciliation_reports"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Coverage history is present; refusing downgrade that would delete accepted baselines or reports")
    for table in ("portfolio_reconciliation_reports", "portfolio_reconciliation_days", "portfolio_reconciliation_cursors", "portfolio_account_baselines"):
        op.drop_table(table)
