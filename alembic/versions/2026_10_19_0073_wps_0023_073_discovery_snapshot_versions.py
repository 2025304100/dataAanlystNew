"""Allow repeated same-day discovery snapshot versions without deleting history."""
from alembic import op
import sqlalchemy as sa


revision = "wps_0023_073_discovery_snapshot_versions"
down_revision = "wps_0023_072_verified_input_precision"
branch_labels = None
depends_on = None

_TABLE = "discovery_score_snapshots"
_CONSTRAINT = "uq_snapshot_scope_status_date"


def upgrade():
    connection = op.get_bind()
    constraints = {item["name"] for item in sa.inspect(connection).get_unique_constraints(_TABLE)}
    if _CONSTRAINT in constraints:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_constraint(_CONSTRAINT, type_="unique")


def downgrade():
    connection = op.get_bind()
    duplicate = connection.execute(sa.text(
        "SELECT 1 FROM discovery_score_snapshots GROUP BY scope, status, trade_date "
        "HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError("Cannot restore obsolete uniqueness without losing historical snapshots; keep the current schema")
    constraints = {item["name"] for item in sa.inspect(connection).get_unique_constraints(_TABLE)}
    if _CONSTRAINT not in constraints:
        with op.batch_alter_table(_TABLE) as batch:
            batch.create_unique_constraint(_CONSTRAINT, ["scope", "status", "trade_date"])
