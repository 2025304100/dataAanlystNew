"""Durable request-to-task receipts for the legacy source recheck endpoint."""
from alembic import op

revision = "wps_0024_077_recheck_requests"
down_revision = "wps_0023_076_inspection_workflow"
branch_labels = depends_on = None


def upgrade():
    from app.models.portfolio_inspection_workflow import ReconciliationRecheckRequest
    ReconciliationRecheckRequest.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Keep original request receipts; application rollback must not delete task identity")
