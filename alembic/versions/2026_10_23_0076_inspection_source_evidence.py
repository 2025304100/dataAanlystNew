"""Transactional reconciliation receipts and immutable evidence associations.

Revision ID: wps_0023_076_inspection_workflow
Revises: wps_0023_075_g5_admission_governance
"""
from alembic import op
import sqlalchemy as sa
revision = "wps_0023_076_inspection_workflow"
down_revision = "wps_0023_075_g5_admission_governance"
branch_labels = depends_on = None


def upgrade():
    from app.models.portfolio_inspection_workflow import ReconciliationSourceEvent, InspectionEvidenceRequest, InspectionEvidenceDecision
    for model in (ReconciliationSourceEvent, InspectionEvidenceRequest, InspectionEvidenceDecision):
        model.__table__.create(op.get_bind(), checkfirst=True)
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("portfolio_reconciliation_days") and "account_source_pending" not in {
            column["name"] for column in inspector.get_columns("portfolio_reconciliation_days")}:
        op.add_column("portfolio_reconciliation_days", sa.Column("account_source_pending", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    raise RuntimeError("Keep source receipts and reviewed evidence history; roll back application code without deleting evidence")
