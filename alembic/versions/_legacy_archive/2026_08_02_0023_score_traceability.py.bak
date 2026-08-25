"""WP7-05: Score 解释追溯字段。

向 scores 表添加 factor_set_id 和 factor_member_versions_json 字段，
用于完整追溯每条 Score 记录对应的 FactorSet 和成员版本快照。

Idempotent: safe to run multiple times.

Revision ID: wps_0023_023_score_traceability
Revises: wps_0023_022_shadow_observations
Create Date: 2026-08-02

对齐 docs/专业因子库开发计划.md §WP7-05 和 docs/因子设置与专业因子库改造方案.md §7.5。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_023_score_traceability"
down_revision: Union[str, None] = "wps_0023_022_shadow_observations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "scores"

NEW_COLUMNS = (
    "factor_set_id",
    "factor_member_versions_json",
)


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def _has_index(inspector, table: str, index_name: str) -> bool:
    return index_name in {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    """Add factor_set_id and factor_member_versions_json to scores."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if TABLE_NAME not in existing_tables:
        # scores 表由更早的迁移创建；不存在时跳过（幂等）
        return

    # factor_set_id: 关联的 FactorSet ID（可为 NULL，兼容 manual 模式）
    if not _has_column(inspector, TABLE_NAME, "factor_set_id"):
        op.add_column(
            TABLE_NAME,
            sa.Column("factor_set_id", sa.String(length=64), nullable=True),
        )

    # factor_member_versions_json: 成员版本快照 JSON
    # {"factor_code": {"version": 1, "role": "feature", "missing_policy": "exclude"}}
    if not _has_column(inspector, TABLE_NAME, "factor_member_versions_json"):
        op.add_column(
            TABLE_NAME,
            sa.Column(
                "factor_member_versions_json",
                sa.Text(),
                nullable=True,
            ),
        )

    # 为 factor_set_id 添加索引（便于按 FactorSet 查询历史 Score）
    if not _has_index(inspector, TABLE_NAME, "ix_scores_factor_set_id"):
        try:
            op.create_index(
                "ix_scores_factor_set_id",
                TABLE_NAME,
                ["factor_set_id"],
            )
        except Exception:
            # 某些数据库（如旧版 SQLite）可能不支持 IF NOT EXISTS，忽略重复创建
            pass


def downgrade() -> None:
    """Remove factor_set_id and factor_member_versions_json from scores."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in inspector.get_table_names():
        return

    existing_indexes = {i["name"] for i in inspector.get_indexes(TABLE_NAME)}
    if "ix_scores_factor_set_id" in existing_indexes:
        try:
            op.drop_index("ix_scores_factor_set_id", table_name=TABLE_NAME)
        except Exception:
            pass

    existing_columns = {c["name"] for c in inspector.get_columns(TABLE_NAME)}
    for column_name in reversed(NEW_COLUMNS):
        if column_name in existing_columns:
            try:
                op.drop_column(TABLE_NAME, column_name)
            except Exception:
                # 降级失败时忽略（保持幂等）
                pass
