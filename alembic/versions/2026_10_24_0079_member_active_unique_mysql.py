"""Enforce one current member while preserving all archived versions.

Both MySQL 5.7 and SQLite use a generated NULL-or-1 slot in the unique index.
Duplicate current rows fail before DDL. A bridge index protects uniqueness
during replacement. Revision 0083 compensates databases that already applied
the earlier drop-only form of this migration.
"""
from alembic import op

revision = "wps_0024_079_member_active_unique_mysql"
down_revision = "wps_0024_078_request_origin"
branch_labels = depends_on = None

_INDEX = "idx_portfolio_members_active"
_TABLE = "portfolio_members"


def _index_exists(bind, table: str, index: str) -> bool:
    from sqlalchemy import inspect

    return index in {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade():
    from app.db.migration_contracts_v83 import ensure_member_contract

    ensure_member_contract(op.get_bind())


def downgrade():
    raise RuntimeError(
        "Retain conditional member uniqueness and SCD2 history; restore a verified backup instead"
    )
