"""给 `factor_audit_logs` 加 `pool_id` 独立列（挖掘域审计）。

Revision ID: wps_0023_053_factor_audit_pool_id
Revises: wps_0023_052_mining_fk_constraints

为什么需要（2026-09-17 需求方裁决 ⑤）
====================================
挖掘域（候选池）的写操作审计原先落在 `factor_audit_logs`，但 `pool_id` 只能塞进
`attributes_json`（见 `candidate_pool/service.py::_write_audit` 旧实现与注释）。

代价（实测确认）：
1. **按池查审计要走 JSON 过滤**，无法用索引 → 池多了以后必然全表扫；
2. 审计检索接口（G4 的 attributes_json 文本检索）把「结构化关联键」和
   「自由备注」混在一列，语义不清。

裁决：加**一列可空的 `pool_id` + 索引**（不新建表）。因子域的审计行该列为 NULL，
语义明确：`pool_id IS NOT NULL` ⇔ 挖掘域审计。

实现要点
========
- 列可空、加索引：对既有 7 行数据零影响（实测该表当前仅 7 行，且非挖掘域）
- **幂等**：`_auto_align_all_schema`（`app/db/init_db.py`）会在启动时静默补列，
  所以真实库可能已有该列 —— 这里先探测再改，避免「重复加列」报错
- 索引名与 ORM 一致（`ix_factor_audit_logs_pool_id`），否则 `verify_schema_drift.py`
  会报漂移（R13 第 ② 条）
- 本表是既有表（已是 InnoDB），`add_column` 不涉及引擎选择（R13 的 `mysql_engine`
  要求针对 `create_table`）

不在本修订范围内
================
- 表字符集统一（utf8 → utf8mb4）：真实库该表为 utf8，属独立议题（已记录）
- `note` 列：本次裁决只加 `pool_id`（自由备注仍走 `attributes_json`）
- 历史行回填 `pool_id`：历史行不改（审计是历史事实）
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_053_factor_audit_pool_id"
down_revision = "wps_0023_052_mining_fk_constraints"
branch_labels = None
depends_on = None

TABLE = "factor_audit_logs"
COLUMN = "pool_id"
INDEX = "ix_factor_audit_logs_pool_id"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column() -> bool:
    insp = _inspector()
    if TABLE not in insp.get_table_names():
        return False
    return any(c["name"] == COLUMN for c in insp.get_columns(TABLE))


def _has_index() -> bool:
    insp = _inspector()
    if TABLE not in insp.get_table_names():
        return False
    return any(i["name"] == INDEX for i in insp.get_indexes(TABLE))


def upgrade() -> None:
    if not _has_column():
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN, sa.String(length=64), nullable=True,
                comment="候选池 ID（挖掘域审计用；因子域为 NULL）",
            ),
        )
    if not _has_index():
        op.create_index(INDEX, TABLE, [COLUMN], unique=False)


def downgrade() -> None:
    """可逆：先删索引再删列（列删除会连带索引，显式分开更清晰）。"""
    if _has_index():
        op.drop_index(INDEX, table_name=TABLE)
    if _has_column():
        op.drop_column(TABLE, COLUMN)
