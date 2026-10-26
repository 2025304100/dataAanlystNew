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
_BACKFILL_BATCH = 500


def _title_hash(title: str | None) -> str:
    return hashlib.md5((title or "").encode("utf-8")).hexdigest()


def _backfill_existing_rows(conn) -> None:
    """分批回填 title_hash：先把结果集取完，再发 UPDATE。

    必须避免在同一条连接上"边读未取完的流式结果边写"：MySQL 驱动会把
    stream_results 映射成服务端游标（PyMySQL SSCursor），下一次命令会先
    丢弃未读完的结果（"Previous unbuffered result was left incomplete"），
    导致只有首行被回填、其余存量行静默保持 NULL。改为 LIMIT 分批取完再写，
    既规避该方言行为，也保证内存有界（每批最多 _BACKFILL_BATCH 行）。
    """
    while True:
        rows = conn.execute(
            sa.text(
                f"SELECT id, title FROM {_TABLE} "
                f"WHERE title_hash IS NULL LIMIT {_BACKFILL_BATCH}"
            )
        ).fetchall()
        if not rows:
            return
        for row in rows:
            conn.execute(
                sa.text(f"UPDATE {_TABLE} SET title_hash = :h WHERE id = :id"),
                {"h": _title_hash(row[1]), "id": row[0]},
            )


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
    # 存量回填：Python 端计算 MD5（方言无关）
    _backfill_existing_rows(conn)


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
