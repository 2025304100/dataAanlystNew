"""TD6: align strategy_execution_snapshots migration chain with ORM authority (add 3 binding columns + 3 indexes).

Revision ID: wps_0023_056_td6_snapshot_binding_columns
Revises: wps_0023_055_accounting_float_to_double

为什么需要（TD6，2026-09-18）
====================================
`strategy_execution_snapshots` 的 `usage_binding_id` / `snapshot_json` /
`content_hash` 三列历史上从未有过任何 alembic add_column 记录（TD5 全库漂移
审计 C2 裁决结论）：线上 MySQL 是靠历史进程的 schema 自动对齐补出来的，迁移链
（0026 显式 op.create_table）产物一直是旧结构。TD6 把 ORM 权威声明统一到
decision_engine 旧类并补上三列后，任何「纯迁移链」环境（CI 全新 sqlite、
G0/G1 契约测试 fixture `tmp_alembic_db`）建出的表都缺这三列，而 service 新契约
（portfolio_factor_usage.save_and_apply）按三列读写 → `no such column` /
`NOT NULL constraint failed`。

本修订让迁移链产物与 ORM 权威类、线上实态三方对齐：
  - 线上 MySQL：三列已存在（TD5 实证）→ 幂等跳过，零操作；
  - 新装/测试环境：补齐三列 + 3 个独立索引
    （ix_..._snapshot_no / ix_..._usage_binding_id / ix_..._content_hash，
    列声明与 app/models/decision_engine.py 保持一致）。

实现要点
========
- 幂等：逐列/逐索引探 information_schema（MySQL）/ PRAGMA（SQLite），已存在即跳过；
- MySQL 走「加 nullable 列 → 回填 → MODIFY NOT NULL」三段式（TEXT 不能带 DEFAULT，
  非空表直接加 NOT NULL 列会失败）；
- SQLite 直接 `NOT NULL DEFAULT ''`（SQLite 要求 ADD COLUMN 的 NOT NULL 列必须
  带非 NULL DEFAULT，即使是空表）；
- usage_binding_id 可空、无外键（照 DB 实态，TD5 SHOW CREATE TABLE 为唯一权威；
  勿想当然补 FK —— service 以 int(pfu.id) 写入，历史数据可能含孤儿）。

DoD 证据
========
- `.workbuddy/mining/verify_schema_drift.py --tables strategy_execution_snapshots`
  drift=0（TD6 DoD① 已验收，本修订不改变线上实态）；
- tests/test_g1_factor_usage.py / tests/test_backtest_detail_snapshot_contract.py
  回归恢复基线水平（TD6 DoD③）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "wps_0023_056_td6_snapshot_binding_columns"
down_revision = "wps_0023_055_accounting_float_to_double"
branch_labels = None
depends_on = None

_TABLE = "strategy_execution_snapshots"

# (列名, SQLite DDL, MySQL nullable 期 DDL, MySQL 最终 NOT NULL DDL, 回填值)
_TARGET_COLUMNS = [
    (
        "usage_binding_id",
        "INTEGER NULL",
        "INTEGER NULL",
        "INTEGER NULL",
        None,
    ),
    (
        "snapshot_json",
        "TEXT NOT NULL DEFAULT ''",
        "TEXT NULL",
        "TEXT NOT NULL",
        "{}",
    ),
    (
        "content_hash",
        "VARCHAR(64) NOT NULL DEFAULT ''",
        "VARCHAR(64) NULL",
        "VARCHAR(64) NOT NULL",
        "''",
    ),
]

_TARGET_INDEXES = [
    "ix_strategy_execution_snapshots_snapshot_no",
    "ix_strategy_execution_snapshots_usage_binding_id",
    "ix_strategy_execution_snapshots_content_hash",
]


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
    is_sqlite = conn.dialect.name == "sqlite"

    cols = _existing_columns(conn)
    if not cols:
        return  # 表本身不存在（防御；0026 已建，正常不会走到）

    for name, sqlite_ddl, my_null_ddl, my_notnull_ddl, backfill in _TARGET_COLUMNS:
        if name in cols:
            continue
        if is_sqlite:
            op.execute(f'ALTER TABLE "{_TABLE}" ADD COLUMN "{name}" {sqlite_ddl}')
        else:
            # MySQL 三段式：nullable 加列 -> 回填 -> MODIFY NOT NULL
            op.execute(f"ALTER TABLE `{_TABLE}` ADD COLUMN `{name}` {my_null_ddl}")
            if backfill is not None:
                op.execute(f"UPDATE `{_TABLE}` SET `{name}` = {backfill} WHERE `{name}` IS NULL")
            op.execute(f"ALTER TABLE `{_TABLE}` MODIFY COLUMN `{name}` {my_notnull_ddl}")

    have_idx = _existing_indexes(conn)
    for idx in _TARGET_INDEXES:
        if idx in have_idx:
            continue
        col = idx[len("ix_strategy_execution_snapshots_"):]
        op.create_index(idx, _TABLE, [col])


def downgrade() -> None:
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == "sqlite"

    have_idx = _existing_indexes(conn)
    for idx in _TARGET_INDEXES:
        if idx in have_idx:
            op.drop_index(idx, table_name=_TABLE)

    cols = _existing_columns(conn)
    for name, *_rest in reversed(_TARGET_COLUMNS):
        if name in cols:
            op.drop_column(_TABLE, name)
