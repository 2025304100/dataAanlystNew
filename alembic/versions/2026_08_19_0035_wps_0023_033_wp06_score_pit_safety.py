"""wps_0023_033_wp06_score_pit_safety_and_published_notnull.

WP0-6 / tasks.md TR-06.5：
  1) scores.published_at 历史 NULL 行回填 = created_at（避免猜真实发布日）
  2) 新增 scores.pit_safety 列（NOT_PIT_SAFE / PIT_VERIFIED），默认 NOT_PIT_SAFE
  3) 旧 pit_flag 到 pit_safety 的一次性映射：
       PIT_SAFE       → PIT_VERIFIED
       NOT_PIT_SAFE   → NOT_PIT_SAFE
       NOT_CHECKED/其他 → NOT_PIT_SAFE（默认保守）
  4) published_at 改为 NOT NULL（SQLite 走 add column + 回填后做重写；MySQL/PG 使用 ALTER）

Revision ID: wps_0023_033_wp06_score_pit_safety
Revises: wps_0023_032_g0_wp02ab_leftovers
Create Date: 2026-08-19 13:05:00.000000
"""
from alembic import op
from sqlalchemy import Column, DateTime, inspect, Integer, String, text


revision = 'wps_0023_033_wp06_score_pit_safety'
down_revision = 'wps_0023_032_g0_wp02ab_leftovers'
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return name in insp.get_table_names()


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    try:
        cols = [c["name"] for c in insp.get_columns(table)]
    except Exception:
        return False
    return column in cols


def upgrade() -> None:
    if not _table_exists("scores"):
        return

    # (1) pit_safety 列：若不存在则新增
    if not _column_exists("scores", "pit_safety"):
        op.add_column(
            "scores",
            Column(
                "pit_safety",
                String(16),
                nullable=False,
                server_default="NOT_PIT_SAFE",
                comment="NOT_PIT_SAFE | PIT_VERIFIED",
            ),
        )

    # (2) 回填 published_at = COALESCE(published_at, created_at)
    #     created_at 永远 NOT NULL，所以处理后 published_at 无 NULL
    op.execute(text("""
        UPDATE scores
        SET published_at = COALESCE(published_at, created_at)
        WHERE published_at IS NULL
    """))

    # (3) 映射 pit_flag → pit_safety（若 pit_flag 存在）
    if _column_exists("scores", "pit_flag"):
        op.execute(text("""
            UPDATE scores
            SET pit_safety = CASE
                WHEN pit_flag = 'PIT_SAFE' THEN 'PIT_VERIFIED'
                WHEN pit_flag = 'NOT_PIT_SAFE' THEN 'NOT_PIT_SAFE'
                ELSE 'NOT_PIT_SAFE'
            END
            WHERE pit_safety IS NULL OR pit_safety NOT IN ('PIT_VERIFIED', 'NOT_PIT_SAFE')
        """))

    # (4) 若存在 first_published_at，且 published_at 当前值仍非首次时间：
    #     first_published_at = COALESCE(first_published_at, published_at)
    if _column_exists("scores", "first_published_at"):
        op.execute(text("""
            UPDATE scores
            SET first_published_at = COALESCE(first_published_at, published_at)
            WHERE first_published_at IS NULL
        """))

    # (5) 建立 published_at + pit_safety 联合索引，方便 DecisionEngine 生产链路过滤
    idx_name = "ix_scores_published_at_pit_safety"
    try:
        op.create_index(idx_name, "scores", ["published_at", "pit_safety"])
    except Exception:
        pass


def downgrade() -> None:
    if not _table_exists("scores"):
        return

    idx_name = "ix_scores_published_at_pit_safety"
    try:
        op.drop_index(idx_name, table_name="scores")
    except Exception:
        pass

    # pit_safety 列保留 (可逆安全；不做 DROP 避免丢数据)
    # 如需强 DROP，可手动：
    #   op.drop_column("scores", "pit_safety")
    pass
