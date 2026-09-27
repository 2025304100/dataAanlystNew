"""T11: 提交去重 + 状态机 API 扩展。

为 backtest_runs 新增状态机与进度跟踪所需的 6 列 + 去重复合索引：
  - frontend_session_id: 去重键（1/5），前端会话 ID
  - stage: 新状态枚举（precheck|creating|queued|running|success|failed|blocked|cancelled）
  - progress_pct: 进度百分比 [0,100]
  - updated_at: 自动更新时间戳
  - error_code: 机器可读错误码
  - retryable: 是否可重试布尔标记

去重索引 ix_runs_dedup_key 覆盖 (frontend_session_id, config_hash,
portfolio_id, start_date, end_date, created_at)，加速 10s 窗口去重查询。

Revision ID: wps_0023_047_bfg_run_stage_and_progress
Revises: wps_0023_046_fix_idempotency_status_index_rollback
Create Date: 2026-09-02 00:49:00
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "wps_0023_047_bfg_run_stage_and_progress"
down_revision: str | None = "wps_0023_046_fix_idempotency_status_index_rollback"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table_name):
        return False
    return any(c.get("name") == column_name for c in insp.get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table_name):
        return False
    return any(i.get("name") == index_name for i in insp.get_indexes(table_name))


def upgrade() -> None:
    table = "backtest_runs"
    new_columns = [
        # name, type, nullable, server_default
        ("frontend_session_id", sa.String(length=64), True, None),
        ("stage", sa.String(length=16), True, sa.text("'queued'")),
        ("progress_pct", sa.Float(), True, sa.text("0.0")),
        ("updated_at", sa.DateTime(), True, None),
        ("error_code", sa.String(length=64), True, None),
        ("retryable", sa.Boolean(), True, sa.text("0")),
    ]
    for col_name, col_type, nullable, srv_default in new_columns:
        if not _column_exists(table, col_name):
            kwargs: dict = {"nullable": nullable}
            if srv_default is not None:
                kwargs["server_default"] = srv_default
            if col_name == "updated_at":
                # onupdate 不可在 server_default DDL 中表达，用 SQLAlchemy 端处理
                pass
            op.add_column(table, sa.Column(col_name, col_type, **kwargs))

    # 去重复合索引（覆盖 5 键 + created_at 时间窗口）
    idx_name = "ix_runs_dedup_key"
    idx_cols = [
        "frontend_session_id",
        "config_hash",
        "portfolio_id",
        "start_date",
        "end_date",
        "created_at",
    ]
    if not _index_exists(table, idx_name):
        # 任一列缺失就跳过（幂等安全）
        insp = sa.inspect(op.get_bind())
        existing_cols = {c["name"] for c in insp.get_columns(table)} if insp.has_table(table) else set()
        if all(str(c) in existing_cols for c in idx_cols):
            try:
                op.create_index(idx_name, table, idx_cols, if_not_exists=True)
            except TypeError:
                op.create_index(idx_name, table, idx_cols)


def downgrade() -> None:
    table = "backtest_runs"
    # 1. 先删索引（对称于 upgrade 顺序）
    idx_name = "ix_runs_dedup_key"
    if _index_exists(table, idx_name):
        # NOTE: MySQL 不支持 DROP INDEX IF EXISTS 语法，因此手工先检查再删除
        op.drop_index(idx_name, table_name=table)

    # 2. 删除 upgrade 新增的 6 列（逆序）
    for col_name in [
        "retryable",
        "error_code",
        "updated_at",
        "progress_pct",
        "stage",
        "frontend_session_id",
    ]:
        if _column_exists(table, col_name):
            op.drop_column(table, col_name)
