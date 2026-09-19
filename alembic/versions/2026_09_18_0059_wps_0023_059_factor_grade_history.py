"""T36: create factor_grade_history (M2 质量分级历史).

Revision ID: wps_0023_059_factor_grade_history
Revises: wps_0023_058_f1_experience_tables

为什么需要（T36，2026-09-18）
============================
质量分级（设计 §7.10 / 需求 §6.8）要求**每次评定落库**：
  - 等级变更可追溯（谁在何时因何理由调整）；
  - 季度重评需要历史序列（B 连续 2 季稳定升 A；S/A 连续 2 季下滑降级并移出
    FactorSet）；
  - `grade_manual_adjusted=1` 的因子不被季度任务覆盖 —— 判定依据落在本表。

列集合/索引与 `app/models/factor_grade_history.py`（ORM 权威）完全对齐：
  - 单列索引 ix_factor_grade_history_factor_version_id（按版本查历史）；
  - 复合索引 ix_factor_grade_history_version_created（版本 + 时间，重评取最近 N 季）。

实现要点
========
- 显式 `mysql_engine="InnoDB"`（R13：默认 MyISAM 无事务、静默吞 FK）；
- **不显式声明 charset** —— 跟随库默认（gpfx=utf8）；显式 utf8mb4 会触发
  verify_schema_drift 字符集 WARN；
- 不建外键（与 0058 同款口径：一致性由 service 层同事务保证）；
- NOT NULL 列不带 server_default（ORM 用 Python 端 default）；
- 幂等防御：表已存在（auto-align 先行环境）则跳过；downgrade 存在才 drop。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "wps_0023_059_factor_grade_history"
down_revision = "wps_0023_058_f1_experience_tables"
branch_labels = None
depends_on = None

# 显式 InnoDB（R13）；charset 跟随库默认，不显式声明。
_INNODB_KWARGS = {"mysql_engine": "InnoDB"}

_TABLE = "factor_grade_history"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa_inspect(bind).get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("factor_version_id", sa.String(64), nullable=False),
        sa.Column("grade", sa.String(2), nullable=False),
        sa.Column("previous_grade", sa.String(2), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("metrics_snapshot_json", sa.Text(), nullable=False),
        sa.Column("thresholds_source", sa.String(16), nullable=True),
        sa.Column("threshold_snapshot_json", sa.Text(), nullable=True),
        sa.Column("source", sa.String(24), nullable=True),
        sa.Column("action", sa.String(16), nullable=True),
        sa.Column("manual_adjusted", sa.Integer(), nullable=True),
        sa.Column("adjusted_by", sa.String(64), nullable=True),
        sa.Column("adjust_reason", sa.Text(), nullable=True),
        sa.Column("removed_from_factor_set", sa.Integer(), nullable=True),
        sa.Column("icir", sa.Float(), nullable=True),
        sa.Column("decay_ratio", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        **_INNODB_KWARGS,
    )
    op.create_index(
        "ix_factor_grade_history_factor_version_id",
        _TABLE, ["factor_version_id"], unique=False)
    op.create_index(
        "ix_factor_grade_history_version_created",
        _TABLE, ["factor_version_id", "created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    names = sa_inspect(bind).get_table_names()
    if _TABLE not in names:
        return
    for idx in ("ix_factor_grade_history_version_created",
                "ix_factor_grade_history_factor_version_id"):
        try:
            op.drop_index(idx, table_name=_TABLE)
        except Exception:
            pass
    op.drop_table(_TABLE)
