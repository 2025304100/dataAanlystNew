"""B1: add 4 quality-grade "current state" columns to factor_versions.

Revision ID: wps_0023_060_factor_version_grade_columns
Revises: wps_0023_059_factor_grade_history

为什么需要（B1，2026-09-20）
============================
M2 质量分级（设计 §7.10 / 需求 §6.8）要求 `grade()` 评定结果落库到
`factor_versions` 本身（当前态），而不是只写历史表：
  - `quality_grade`：S/A/B/C/D；
  - `grade_updated_at`：最近一次评定时点；
  - `grade_metrics_json`：评定依据快照（grade/reason/metrics）；
  - `grade_manual_adjusted`：=1 表示人工调整，季度重评任务读它跳过
    （`review_quarterly(manual_adjusted=True)` 判守，需求 §6.8）。

历史序列继续走 `factor_grade_history`（0059），本修订只加「当前态」4 列。

实现要点
========
- 全部 **nullable、无 server_default、无外键、无索引**（B1 最小面；
  `grade_manual_adjusted` 的 0 由 ORM Python 端 default 兜底 —— 老行未评定时语义
  即「未人工调整」）；B2 需要按等级筛选时再加索引，避免空索引。
- 幂等：逐列探 inspector，已存在即跳过（线上 MySQL 可能已被业务/auto-align 补齐）；
- SQLite / MySQL 均用 `ALTER TABLE ADD COLUMN`（nullable TEXT/VARCHAR/DATETIME/
  INTEGER 两方言都直接支持）；
- 不显式 charset（保持库默认；显式 utf8mb4 触发 verify_schema_drift 字符集 WARN）。

DoD 证据
========
- `tests/services/factors/mining/test_grade_columns_b1.py`：升/降级 + drift=0 + 落库；
- `.workbuddy/mining/verify_schema_drift.py --tables factor_versions`（sqlite）drift=0。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "wps_0023_060_factor_version_grade_columns"
down_revision = "wps_0023_059_factor_grade_history"
branch_labels = None
depends_on = None

_TABLE = "factor_versions"

# (列名, DDL) —— 全部 nullable（SQLite ADD COLUMN 的 NOT NULL 需要 DEFAULT，
# 这里统一 nullable 最稳，ORM 端 default 由 Python 兜底）
_TARGET_COLUMNS = [
    ("quality_grade", "VARCHAR(2)"),
    ("grade_updated_at", "DATETIME"),
    ("grade_metrics_json", "TEXT"),
    ("grade_manual_adjusted", "INTEGER"),
]

#: 漂移治理（B1 drift DoD）：ORM 的 `effective_from(index=True)` 在迁移链里
#: 从未建索引（旧版 0021 只建了表与其它索引，生产靠 auto-align 兜底补齐）。
#: 本修订对老库幂等，对全新链建出与 ORM 声明一致的索引 → verify_schema_drift=0。
_TARGET_INDEXES = ["ix_factor_versions_effective_from"]


def _existing_columns(conn) -> set:
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(conn)
    if _TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(_TABLE)}


def _existing_indexes(conn) -> set:
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(conn)
    if _TABLE not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    # 方言分支（与 0060 同款）：SQLite 用双引号，MySQL 必须用反引号标识符，
    # 否则 `ALTER TABLE "t" ...` 在 MySQL 被当字符串字面量报 1064。
    is_sqlite = conn.dialect.name == "sqlite"
    q = '"' if is_sqlite else "`"
    cols = _existing_columns(conn)
    for name, ddl in _TARGET_COLUMNS:
        if name in cols:
            continue
        op.execute(f"ALTER TABLE {q}{_TABLE}{q} "
                   f"ADD COLUMN {q}{name}{q} {ddl}")

    have_idx = _existing_indexes(conn)
    for idx in _TARGET_INDEXES:
        if idx in have_idx:
            continue
        col = idx[len("ix_factor_versions_"):]
        op.create_index(idx, _TABLE, [col])


def downgrade() -> None:
    conn = op.get_bind()
    have_idx = _existing_indexes(conn)
    for idx in _TARGET_INDEXES:
        if idx in have_idx:
            op.drop_index(idx, table_name=_TABLE)

    cols = _existing_columns(conn)
    for name, _ddl in reversed(_TARGET_COLUMNS):
        if name in cols:
            op.drop_column(_TABLE, name)