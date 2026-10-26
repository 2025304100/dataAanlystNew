"""Track explicit sector-label origin without guessing historical overrides."""
import sqlalchemy as sa
from alembic import op

revision = "wps_0024_090_market_sector_source"
down_revision = "wps_0024_089_ai_session_surface"
branch_labels = depends_on = None


def upgrade():
    conn = op.get_bind()
    # The original migration retains unused event_type/event_date as NOT NULL.
    # Current ORM writers do not invent these legacy facts. MySQL strict mode
    # therefore needs the same retain-and-relax contract used by other domains.
    from app.db.market_event_schema import ensure_market_event_legacy_writes
    ensure_market_event_legacy_writes(conn)
    inspector = sa.inspect(conn)
    if inspector.has_table("market_events") and "sector_tag_source" not in {
        column["name"] for column in inspector.get_columns("market_events")
    }:
        op.add_column("market_events", sa.Column("sector_tag_source", sa.String(16), nullable=True))


def downgrade():
    # Keep retired columns nullable: restoring obsolete NOT NULL requirements
    # would reject rows created after upgrade and also break the prior ORM.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table("market_events") and "sector_tag_source" in {
        column["name"] for column in inspector.get_columns("market_events")
    }:
        op.drop_column("market_events", "sector_tag_source")
