"""wps_0023_049_factor_weight_snapshots (Task 11 / FR-12 / AC-11).

将旧 factor_weight_snapshots 表（per-factor 细粒度行，rev 006 创建）替换为
per-model 聚合快照表（model_id PK + 集合元数据 + 8 个 JSON 列 + 6 个 DATE 列）。

训练成功时 INSERT 1 行，用于历史模型即使在集合废弃/修改后仍能稳定还原
权重→版本→集合的全链血缘（AC-11 不漂移）。

三段闭环：upgrade head -> downgrade to 048 -> upgrade head，每个阶段 exit=0。

Revision ID: wps_0023_049_factor_weight_snapshots
Revises: wps_0023_048_factor_audit_logs
Create Date: 2026-09-02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "wps_0023_049_factor_weight_snapshots"
down_revision: str | None = "wps_0023_048_factor_audit_logs"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ── 先删除旧 per-factor 结构（rev 006 建；字段与 Task11 新表不兼容） ──
    # SQLite / MySQL 通用：先删索引再删表（if_exists 兼容已有干净环境）
    try:
        op.drop_index("ix_factor_weight_snapshots_factor_code", table_name="factor_weight_snapshots")
    except Exception:
        pass
    try:
        op.drop_index("ix_factor_weight_snapshots_model_run_id", table_name="factor_weight_snapshots")
    except Exception:
        pass
    try:
        op.drop_table("factor_weight_snapshots")
    except Exception:
        pass

    # ── 新建 per-model 聚合快照表（Task 11 spec 要求 ≥13 字段） ──
    op.create_table(
        "factor_weight_snapshots",
        sa.Column("model_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("factor_set_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("factor_set_content_hash", sa.String(length=64), nullable=True),
        # 4 个成员元数据 JSON（数组型） — MySQL 原生 JSON / SQLite TEXT(JSON semantics)
        sa.Column("factor_ids_json", sa.Text(), nullable=False, default="[]"),
        sa.Column("factor_version_ids_json", sa.Text(), nullable=False, default="[]"),
        sa.Column("roles_json", sa.Text(), nullable=False, default="[]"),
        sa.Column("constraints_json", sa.Text(), nullable=False, default="[]"),
        sa.Column("missing_strategies_json", sa.Text(), nullable=False, default="[]"),
        # 6 个 DATE 列（训练/验证窗口 + 数据截止）
        sa.Column("train_start_date", sa.Date(), nullable=True),
        sa.Column("train_end_date", sa.Date(), nullable=True),
        sa.Column("valid_start_date", sa.Date(), nullable=True),
        sa.Column("valid_end_date", sa.Date(), nullable=True),
        sa.Column("data_cutoff_date", sa.Date(), nullable=False),
        # 权重 JSON：raw 可能很长 → LONGTEXT semantic；norm JSON 数组
        sa.Column("weights_raw_json", sa.Text(), nullable=False, default="{}"),
        sa.Column("weights_norm_json", sa.Text(), nullable=False, default="[]"),
        # 训练模式（ridge_offline / ridge_warehouse / …）
        sa.Column("train_mode", sa.String(length=64), nullable=False, index=True),
        # 不可变创建时间（幂等 upsert 不更新）
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )


def downgrade() -> None:
    # ── 删除新聚合表 ──
    op.drop_table("factor_weight_snapshots")

    # ── 恢复旧 per-factor 结构（与 rev 006 完全一致，保证 chain 可逆） ──
    op.create_table(
        "factor_weight_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "model_run_id",
            sa.String(length=64),
            sa.ForeignKey("factor_model_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("factor_code", sa.String(length=64), nullable=False),
        sa.Column("factor_version", sa.Integer(), nullable=False),
        sa.Column("coefficient", sa.Float(), nullable=False),
        sa.Column("normalized_weight", sa.Float(), nullable=False),
        sa.Column("train_ic", sa.Float(), nullable=True),
        sa.Column("validation_ic", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("model_run_id", "factor_code", "factor_version", name="uq_factor_model_weight"),
    )
    op.create_index(
        "ix_factor_weight_snapshots_model_run_id",
        "factor_weight_snapshots",
        ["model_run_id"],
    )
    op.create_index(
        "ix_factor_weight_snapshots_factor_code",
        "factor_weight_snapshots",
        ["factor_code"],
    )
