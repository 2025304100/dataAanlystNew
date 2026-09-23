"""Fix: snapshot members_json Text → MEDIUMTEXT (MySQL) to stop truncation.

Revision ID: wps_0023_061_snapshot_members_json_mediumtext
Revises: wps_0023_060_factor_version_grade_columns

为什么需要（2026-09-21，浏览器走查捕获的真实线上缺陷）
========================================================
向导 Step1「生成挖掘物料」对全市场候选池（5544 只）执行分析时，
后端 `analyze_pool` 读取 `training_candidate_pool_snapshots.members_json`
抛 `JSONDecodeError: Unterminated string starting at char 63168`。

根因：MySQL `Text` 上限 65535 字节，数千成员的 JSON 序列化（symbol/name/
market 等逐字段）超限后被 MySQL **静默截断** → 落库的是非法 JSON →
`json.loads` 崩溃 → 快照分析 500，向导「生成挖掘物料」不可用（1.8 走查 FAIL）。

修复：`members_json` 改为 MEDIUMTEXT（16MB，约 10 万+ 成员绰绰有余）。
SQLite 的 TEXT 本无长度上限，故仅在 MySQL 执行 MODIFY，SQLite 脚本无操作。

实现要点
========
- MySQL：`ALTER TABLE ... MODIFY members_json MEDIUMTEXT NOT NULL`；
  幂等：探列类型已为 MEDIUMTEXT 即跳过；
- SQLite：无操作（schema 已兼容 ORM 声明）；
- 不显式 charset（保持库默认，避免 verify_schema_drift 字符集 WARN）。

DoD 证据
========
- 走查复跑：Step1 生成挖掘物料（全市场 5544 只）→ 锁定 → 看板 全通过；
- `pytest tests/services/factors/candidate_pool`（大成员往返 + analyze 不崩）。
"""
from __future__ import annotations

from alembic import op

revision = "wps_0023_061_snapshot_members_json_mediumtext"
down_revision = "wps_0023_060_factor_version_grade_columns"
branch_labels = None
depends_on = None

_TABLE = "training_candidate_pool_snapshots"
_COLUMN = "members_json"


def _mysql_column_type(bind) -> str | None:
    row = bind.exec_driver_sql(
        "SELECT DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (_TABLE, _COLUMN),
    ).fetchone()
    return str(row[0]).upper() if row else None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        # SQLite TEXT 无长度上限，跳过
        return
    existing = _mysql_column_type(bind)
    if existing == "MEDIUMTEXT":
        return
    op.execute(
        f"ALTER TABLE {_TABLE} MODIFY {_COLUMN} MEDIUMTEXT NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.execute(
        f"ALTER TABLE {_TABLE} MODIFY {_COLUMN} TEXT NOT NULL"
    )