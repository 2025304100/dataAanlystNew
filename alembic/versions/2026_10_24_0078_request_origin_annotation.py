"""Append-only provenance notes for retained reconciliation requests."""
from alembic import op

revision = "wps_0024_078_request_origin"
down_revision = "wps_0024_077_recheck_requests"
branch_labels = depends_on = None


def upgrade():
    from app.models.portfolio_inspection_workflow import ReconciliationRequestAnnotation
    ReconciliationRequestAnnotation.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Retain request provenance; do not delete acceptance evidence")
