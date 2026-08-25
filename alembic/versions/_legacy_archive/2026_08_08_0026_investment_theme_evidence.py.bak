"""Add governed investment themes, symbol mappings and catalyst evidence.

Revision ID: wps_0026_026_investment_theme_evidence
Revises: wps_0025_025_portfolio_candidate_admission_evidence
"""

from alembic import op
import sqlalchemy as sa


revision = "wps_0026_026_investment_theme_evidence"
down_revision = "wps_0025_025_portfolio_candidate_admission_evidence"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "investment_themes" not in tables:
        op.create_table(
            "investment_themes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(64), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("is_active", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("code"),
            sa.UniqueConstraint("name"),
        )
        op.create_index("ix_investment_themes_code", "investment_themes", ["code"])
        op.create_index("ix_investment_themes_name", "investment_themes", ["name"])
        op.create_index("ix_investment_themes_is_active", "investment_themes", ["is_active"])
    if "symbol_theme_mappings" not in tables:
        op.create_table(
            "symbol_theme_mappings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("theme_id", sa.Integer(), sa.ForeignKey("investment_themes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("symbol_id", sa.Integer(), sa.ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_ref", sa.String(256), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("is_confirmed", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("theme_id", "symbol_id", name="uq_symbol_theme_mapping"),
        )
        op.create_index("ix_symbol_theme_mappings_theme_id", "symbol_theme_mappings", ["theme_id"])
        op.create_index("ix_symbol_theme_mappings_symbol_id", "symbol_theme_mappings", ["symbol_id"])
        op.create_index("ix_symbol_theme_mappings_is_confirmed", "symbol_theme_mappings", ["is_confirmed"])
    if "theme_catalysts" not in tables:
        op.create_table(
            "theme_catalysts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("theme_id", sa.Integer(), sa.ForeignKey("investment_themes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("market_event_id", sa.Integer(), sa.ForeignKey("market_events.id", ondelete="SET NULL"), nullable=True),
            sa.Column("title_snapshot", sa.Text(), nullable=False),
            sa.Column("catalyst_score", sa.Float(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_ref", sa.String(512), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("theme_id", "market_event_id", name="uq_theme_market_event_catalyst"),
        )
        op.create_index("ix_theme_catalysts_theme_id", "theme_catalysts", ["theme_id"])
        op.create_index("ix_theme_catalysts_market_event_id", "theme_catalysts", ["market_event_id"])
        op.create_index("ix_theme_catalysts_published_at", "theme_catalysts", ["published_at"])
        op.create_index("ix_theme_catalysts_expires_at", "theme_catalysts", ["expires_at"])


def downgrade():
    op.drop_table("theme_catalysts")
    op.drop_table("symbol_theme_mappings")
    op.drop_table("investment_themes")
