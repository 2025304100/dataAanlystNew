"""Add HF-07 append-only admission controls; leave all trading facts intact.

Installing the schema does not inventory, quarantine, rewrite reports, or enable
new admission. An installation without a reviewed RELEASE remains frozen.
"""
from alembic import op
import sqlalchemy as sa

revision = "wps_0023_075_g5_admission_governance"
down_revision = "wps_0023_074_reconciliation_coverage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("g5_admission_controls",
        sa.Column("scope_key", sa.String(64), primary_key=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("last_event_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("row_version >= 0", name="ck_g5_control_version"))
    op.create_table("g5_admission_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), sa.ForeignKey("portfolios.id", ondelete="RESTRICT")),
        sa.Column("report_id", sa.Integer(), sa.ForeignKey("g5_dual_run_reports.id", ondelete="RESTRICT")),
        sa.Column("original_report_hash", sa.String(64)),
        sa.Column("inventory_snapshot_id", sa.String(64)),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("operator_id", sa.String(128), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("previous_event_id", sa.Integer(), sa.ForeignKey("g5_admission_events.id", ondelete="RESTRICT")),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("audit_event_id", sa.Integer(), sa.ForeignKey("data_governance_audit_events.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("event_type IN ('FREEZE','RELEASE','INVALIDATE_REPORT','RESTORE_REPORT')", name="ck_g5_admission_event_type"))
    op.create_index("ix_g5_admission_scope_id", "g5_admission_events", ["scope_key", "id"])
    op.create_index("ix_g5_admission_report_id", "g5_admission_events", ["report_id", "id"])


def downgrade():
    # Removing reviewed controls would erase rollback/evidence history. Roll
    # application code back with new admission frozen; preserve these tables.
    raise RuntimeError("HF-07 governance history must be preserved; use an audited freeze for application rollback")
