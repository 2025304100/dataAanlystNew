"""Add the PortfolioRule exit configuration required by the runtime ORM.

Revision ID: wps_0023_034_portfolio_rule_exit_config
Revises: wps_0023_033_wp06_score_pit_safety
Create Date: 2026-08-19 17:10:00.000000
"""
from alembic import op
from sqlalchemy import Column, Text, inspect


revision = "wps_0023_034_portfolio_rule_exit_config"
down_revision = "wps_0023_033_wp06_score_pit_safety"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return table in inspector.get_table_names() and column in {
        item["name"] for item in inspector.get_columns(table)
    }


def upgrade() -> None:
    if not _column_exists("portfolio_rules", "exit_config_json"):
        op.add_column("portfolio_rules", Column("exit_config_json", Text, nullable=True))


def downgrade() -> None:
    # Keep user rule history intact on downgrade; this field is optional.
    pass
