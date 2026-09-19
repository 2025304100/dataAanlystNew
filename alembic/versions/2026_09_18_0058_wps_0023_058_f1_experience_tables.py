"""T26/F1: create factor_experience + 3 child tables (4 tables, no FK).

Revision ID: wps_0023_058_f1_experience_tables
Revises: wps_0023_056_td6_snapshot_binding_columns

为什么需要（T26，2026-09-18）
====================================
F1 历史经验库（设计文档 §6.14 / 开发文档 §3.12）4 张表的建表修订：

  - factor_experience            主表（模板+三层指纹+统计回写）
  - factor_experience_tags       标签
  - factor_experience_metrics    指标历史
  - factor_experience_field_deps 字段依赖

列集合/索引名与 `app/models/factor_experience.py`（ORM 权威）完全对齐：
  - 唯一索引 ix_factor_experience_fingerprint（三层指纹全库去重键）；
  - 复合索引 ix_factor_experience_category_status /
    ix_factor_experience_source_success_rate（抽取过滤路径）；
  - 子表 experience_id 单列索引 + tags 的 (experience_id, tag_key) 复合索引。

实现要点
========
- 显式 `mysql_engine="InnoDB"`（R13：默认 MyISAM 无事务、静默吞 FK）；
- 按设计文档**不建外键**（共享库预留、经验可独立存在；一致性由 service
  层同事务写入保证）；
- NOT NULL 列不带 server_default —— 与 ORM 一致（ORM 用 Python 端 default），
  插入一律走 ORM/带值 SQL；
- 幂等防御：表已存在（auto-align 先行环境）则跳过；downgrade 逐表存在才 drop。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "wps_0023_058_f1_experience_tables"
down_revision = "wps_0023_056_td6_snapshot_binding_columns"
branch_labels = None
depends_on = None

# 显式 InnoDB（R13）；**不显式声明 charset** —— 跟随库默认（gpfx=utf8），
# 与全库既有表惯例一致（显式 utf8mb4 会触发 verify_schema_drift 字符集 WARN）。
_INNODB_KWARGS = {"mysql_engine": "InnoDB"}


def _existing_tables(conn) -> set:
    return set(sa_inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    have = _existing_tables(conn)

    if "factor_experience" not in have:
        op.create_table(
            "factor_experience",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("formula_template", sa.Text(), nullable=False),
            sa.Column("formula_ast", sa.Text(), nullable=False),
            sa.Column("category", sa.String(24), nullable=False),
            sa.Column("complexity_json", sa.Text(), nullable=False),
            sa.Column("source", sa.String(24), nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("param_placeholders_json", sa.Text(), nullable=True),
            sa.Column("use_count", sa.Integer(), nullable=False),
            sa.Column("success_count", sa.Integer(), nullable=False),
            sa.Column("success_rate", sa.Float(), nullable=False),
            sa.Column("avg_icir", sa.Float(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("is_negative_sample", sa.Integer(), nullable=False),
            sa.Column("origin_project_id", sa.String(64), nullable=True),
            sa.Column("schema_version", sa.String(8), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            **_INNODB_KWARGS,
        )
        op.create_index(
            "ix_factor_experience_fingerprint", "factor_experience",
            ["fingerprint"], unique=True)
        op.create_index(
            "ix_factor_experience_category_status", "factor_experience",
            ["category", "status"])
        op.create_index(
            "ix_factor_experience_source_success_rate", "factor_experience",
            ["source", "success_rate"])

    if "factor_experience_tags" not in have:
        op.create_table(
            "factor_experience_tags",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("experience_id", sa.String(64), nullable=False),
            sa.Column("tag_key", sa.String(64), nullable=False),
            sa.Column("tag_value", sa.String(255), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            **_INNODB_KWARGS,
        )
        op.create_index(
            "ix_factor_experience_tags_experience_id", "factor_experience_tags",
            ["experience_id"])
        op.create_index(
            "ix_factor_experience_tags_exp_key", "factor_experience_tags",
            ["experience_id", "tag_key"])

    if "factor_experience_metrics" not in have:
        op.create_table(
            "factor_experience_metrics",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("experience_id", sa.String(64), nullable=False),
            sa.Column("metric_type", sa.String(24), nullable=False),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("period", sa.String(32), nullable=True),
            sa.Column("is_oos", sa.Integer(), nullable=False),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            **_INNODB_KWARGS,
        )
        op.create_index(
            "ix_factor_experience_metrics_experience_id", "factor_experience_metrics",
            ["experience_id"])

    if "factor_experience_field_deps" not in have:
        op.create_table(
            "factor_experience_field_deps",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("experience_id", sa.String(64), nullable=False),
            sa.Column("field_code", sa.String(64), nullable=False),
            sa.Column("field_layer", sa.String(8), nullable=False),
            sa.Column("is_required", sa.Integer(), nullable=False),
            **_INNODB_KWARGS,
        )
        op.create_index(
            "ix_factor_experience_field_deps_experience_id",
            "factor_experience_field_deps", ["experience_id"])


def downgrade() -> None:
    conn = op.get_bind()
    have = _existing_tables(conn)
    for table in (
        "factor_experience_field_deps",
        "factor_experience_metrics",
        "factor_experience_tags",
        "factor_experience",
    ):
        if table in have:
            op.drop_table(table)
