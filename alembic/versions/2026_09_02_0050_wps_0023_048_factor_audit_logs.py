"""wps_0023_048_factor_audit_logs (Task 4 / FR-13 / AC-18).

创建 factor_audit_logs 表，作为 P0 factor-center 所有写操作（freeze/train/activate/fallback/clone_set）的
统一审计载体（字段 actor + before_json + after_json + timestamp 非空）。

Revision ID: wps_0023_048_factor_audit_logs
Revises: wps_0023_047_bfg_run_stage_and_progress
Create Date: 2026-09-02

⚠️ 2026-09-18（TD1）原地修订：create_table 补 `mysql_engine="InnoDB"`。
本机 MySQL server 默认引擎是 MyISAM（实测 5.7.26），首版未声明引擎 → 全新安装时
model_run_id 上声明的 FK 会被 MyISAM **静默吞掉**（真实库已中招，由 0056 回填；
与 T01 修 0053 / T07 补 0054 同一病因链）。create_table 只在全新安装执行，
对已应用库零影响。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "wps_0023_048_factor_audit_logs"
down_revision: str | None = "wps_0023_047_bfg_run_stage_and_progress"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # factor_audit_logs（AC-18：before_json/after_json NOT NULL；MySQL BLOB/TEXT 禁止 DEFAULT，
    # 因此不在此处定义 server_default，而在 ORM default='{}' 兜底，保证插入层非空。）
    op.create_table(
        "factor_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False, index=True),
        sa.Column("factor_set_id", sa.String(length=64), nullable=True, index=True),
        sa.Column(
            "model_run_id",
            sa.String(length=64),
            sa.ForeignKey("factor_model_runs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("actor", sa.String(length=128), nullable=False, index=True),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("after_json", sa.Text(), nullable=False),
        sa.Column("attributes_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        # TD1（2026-09-18）：server 默认引擎是 MyISAM，不显式声明会被静默吞掉上面的 FK 定义
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    op.drop_table("factor_audit_logs")
