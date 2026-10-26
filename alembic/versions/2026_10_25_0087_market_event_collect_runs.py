"""market_event_collect_runs: collection observability ledger (P1-7).

New table only; no changes to existing tables. Downgrade drops the ledger
(operational stats, not business data — safe to discard).
"""
import sqlalchemy as sa
from alembic import op

revision = "wps_0024_087_market_event_collect_runs"
down_revision = "wps_0024_086_market_events_title_hash"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "market_event_collect_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trigger_source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("sources_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("collected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_duplicate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="done"),
        sa.Column("errors_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_mecr_trigger_source", "market_event_collect_runs", ["trigger_source"])
    op.create_index("ix_mecr_status", "market_event_collect_runs", ["status"])
    op.create_index("ix_mecr_created_at", "market_event_collect_runs", ["created_at"])


def downgrade():
    op.drop_index("ix_mecr_created_at", table_name="market_event_collect_runs")
    op.drop_index("ix_mecr_status", table_name="market_event_collect_runs")
    op.drop_index("ix_mecr_trigger_source", table_name="market_event_collect_runs")
    op.drop_table("market_event_collect_runs")
