"""Repair legacy write constraints even when the original 0090 was applied.

The first 0090 only added sector_tag_source. Replaying upgrade head does not
rerun that revision, so this independent revision applies the compatibility
repair to those installations too. Existing values and label origins survive.
"""
from alembic import op

from app.db.market_event_schema import ensure_market_event_legacy_writes

revision = "wps_0024_091_market_compat"
down_revision = "wps_0024_090_market_sector_source"
branch_labels = depends_on = None


def upgrade():
    ensure_market_event_legacy_writes(op.get_bind())


def downgrade():
    # Data-safe compatibility rollback: keep the nullable legacy columns.
    # Restoring obsolete NOT NULL constraints would reject legitimate rows
    # written since upgrade, and also break the preceding application's ORM.
    pass
