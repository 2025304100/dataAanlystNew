"""sector_concept_configs: DB-backed sector keyword overrides (P2-12).

New table only. DB rows override built-in keyword lists per sector_id
(whole-list replace); deleting a row restores the built-in default.
Downgrade drops the override table — built-in keywords remain in code.
"""
import sqlalchemy as sa
from alembic import op

revision = "wps_0024_088_sector_concept_configs"
down_revision = "wps_0024_087_market_event_collect_runs"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "sector_concept_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sector_id", sa.String(length=64), nullable=False),
        sa.Column("keywords", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sector_concept_configs_sector_id", "sector_concept_configs", ["sector_id"], unique=True)


def downgrade():
    op.drop_index("ix_sector_concept_configs_sector_id", table_name="sector_concept_configs")
    op.drop_table("sector_concept_configs")
