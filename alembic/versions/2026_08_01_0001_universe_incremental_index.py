"""Add the covering index used by universe incremental sync.

Revision ID: wps_0801_001_universe_incremental_index
Revises: wps_0023_020_api_deprecation_logs
Create Date: 2026-08-01 18:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0801_001_universe_incremental_index"
down_revision: Union[str, None] = "wps_0023_020_api_deprecation_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_universe_incremental_pending"
TABLE_NAME = "universe_symbols"
INDEX_COLUMNS = [
    "region",
    "asset_type",
    "is_synced",
    "last_bar_date",
    "last_synced_at",
    "sync_failed",
]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    existing_indexes = {
        item["name"] for item in inspector.get_indexes(TABLE_NAME)
    }
    if INDEX_NAME not in existing_indexes:
        op.create_index(INDEX_NAME, TABLE_NAME, INDEX_COLUMNS, unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    existing_indexes = {
        item["name"] for item in inspector.get_indexes(TABLE_NAME)
    }
    if INDEX_NAME in existing_indexes:
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
