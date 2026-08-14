"""Add portfolio-scoped candidate pools.

Revision ID: wps_0024_024_portfolio_candidate_pool
Revises: wps_0023_023_score_traceability
"""
from alembic import op
import sqlalchemy as sa

revision = "wps_0024_024_portfolio_candidate_pool"
down_revision = "wps_0023_023_score_traceability"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "portfolio_candidates" in inspector.get_table_names():
        return
    op.create_table(
        "portfolio_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol_id", sa.Integer(), sa.ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_candidate_id", sa.Integer(), nullable=True),
        sa.Column("source_type", sa.String(length=16), nullable=True),
        sa.Column("source_scan_run_id", sa.Integer(), nullable=True),
        sa.Column("pool_memberships_json", sa.Text(), nullable=True),
        sa.Column("admission_snapshot_json", sa.Text(), nullable=True),
        sa.Column("priority_score", sa.Float(), nullable=True),
        sa.Column("recommended_position_pct", sa.Float(), nullable=True),
        sa.Column("factor_tag", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("portfolio_id", "symbol_id", name="uq_portfolio_candidate_symbol"),
    )
    op.create_index("ix_portfolio_candidates_portfolio_id", "portfolio_candidates", ["portfolio_id"])
    op.create_index("ix_portfolio_candidates_symbol_id", "portfolio_candidates", ["symbol_id"])


def downgrade():
    op.drop_table("portfolio_candidates")
