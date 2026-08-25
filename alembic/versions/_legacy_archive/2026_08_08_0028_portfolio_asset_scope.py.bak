"""Make the portfolio asset universe an explicit, enforceable constraint.

Revision ID: wps_0028_028_portfolio_asset_scope
Revises: wps_0027_027_merge_candidate_theme_heads
"""

from alembic import op
import sqlalchemy as sa


revision = "wps_0028_028_portfolio_asset_scope"
down_revision = "wps_0027_027_merge_candidate_theme_heads"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("portfolios")}
    if "asset_scope" not in columns:
        op.add_column(
            "portfolios",
            sa.Column("asset_scope", sa.String(length=16), nullable=False, server_default="mixed"),
        )
        op.create_index("ix_portfolios_asset_scope", "portfolios", ["asset_scope"])
    # Existing portfolios may already contain either asset; mixed preserves them
    # safely until their owner explicitly creates a single-asset strategy.
    op.execute("UPDATE portfolios SET asset_scope = 'mixed' WHERE asset_scope IS NULL OR asset_scope NOT IN ('stock', 'etf', 'mixed')")


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("portfolios")}
    if "asset_scope" in columns:
        indexes = {index["name"] for index in inspector.get_indexes("portfolios")}
        if "ix_portfolios_asset_scope" in indexes:
            op.drop_index("ix_portfolios_asset_scope", table_name="portfolios")
        op.drop_column("portfolios", "asset_scope")
