"""Apply the frozen catch-up inventory with explicit safe backfills.

Already-applied copies receive compensating checks in 0083. Never regenerate
the frozen snapshot from future runtime models; use a new revision instead.
"""
from alembic import op

revision = "wps_0024_080_orm_alignment_catchup"
down_revision = "wps_0024_079_member_active_unique_mysql"
branch_labels = depends_on = None


def upgrade():
    from app.db.migration_schema_v80 import apply_frozen_schema

    apply_frozen_schema(op.get_bind())


def downgrade():
    # Alignment does not own the business facts; never drop them on downgrade.
    pass
