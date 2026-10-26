"""market_events.title_hash: persist title MD5 for indexed collection dedup (P0-4).

Backfills existing rows in Python so the logic is identical on MySQL 5.7 and
SQLite (MySQL's MD5() builtin does not exist in SQLite). Non-unique index:
legacy duplicate titles are tolerated; dedup remains an application-level
check, matching the previous runtime-set semantics.
"""
import hashlib

import sqlalchemy as sa
from alembic import op

revision = "wps_0024_086_market_events_title_hash"
down_revision = "wps_0024_085_legacy_dates"
branch_labels = depends_on = None

_TABLE = "market_events"
_INDEX = "ix_market_events_title_hash"


def _title_hash(title: str | None) -> str:
    return hashlib.md5((title or "").encode("utf-8")).hexdigest()


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return  # create_all 环境由模型直接建表
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "title_hash" not in columns:
        op.add_column(_TABLE, sa.Column("title_hash", sa.String(length=32), nullable=True))
    indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, ["title_hash"])
    # 存量回填：分批拉取 NULL 行，Python 端计算 MD5（方言无关）
    rows = conn.execution_options(stream_results=True).execute(
        sa.text(f"SELECT id, title FROM {_TABLE} WHERE title_hash IS NULL")
    )
    for row in rows:
        conn.execute(
            sa.text(f"UPDATE {_TABLE} SET title_hash = :h WHERE id = :id"),
            {"h": _title_hash(row[1]), "id": row[0]},
        )


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "title_hash" in columns:
        op.drop_column(_TABLE, "title_hash")
