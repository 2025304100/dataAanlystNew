"""Persist portfolio candidate admission evidence for existing installations.

Revision ID: wps_0025_025_portfolio_candidate_admission_evidence
Revises: wps_0024_024_portfolio_candidate_pool
"""

from alembic import op
import sqlalchemy as sa


revision = "wps_0025_025_portfolio_candidate_admission_evidence"
down_revision = "wps_0024_024_portfolio_candidate_pool"
branch_labels = None
depends_on = None


_COLUMNS = {
    "source_type": sa.String(length=16),
    "source_scan_run_id": sa.Integer(),
    "pool_memberships_json": sa.Text(),
    "admission_snapshot_json": sa.Text(),
}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "portfolio_candidates" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("portfolio_candidates")}
    for name, column_type in _COLUMNS.items():
        if name not in existing:
            op.add_column("portfolio_candidates", sa.Column(name, column_type, nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "portfolio_candidates" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("portfolio_candidates")}
    for name in reversed(list(_COLUMNS)):
        if name in existing:
            op.drop_column("portfolio_candidates", name)
