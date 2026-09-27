"""PT-DEF-9 收口：回填 alert_events 的 WP-MSG.1 去重三列并补建唯一索引。

Revision ID: wps_0023_062_alert_events_dedupe_backfill
Revises: wps_0023_061_snapshot_members_json_mediumtext

为什么需要（2026-09-27，组合交易域体检报告 §七.9）
====================================================
`models/alert.py` 声明了 `dedupe_key` / `window_start_at` / `incident_no` 三列
（均 NOT NULL、无 server_default）与唯一约束
`uq_alert_events_dedupe_incident(dedupe_key, incident_no)`，但全库唯一的告警写入
口 `app/services/alerts.py::_fire_event` 从未计算它们。SQLite 上插入直接报 NOT NULL
失败；线上 MySQL 因 `manager.py` 把会话 `sql_mode` 设为非严格
（`NO_ENGINE_SUBSTITUTION`，无 STRICT_TRANS_TABLES）而是静默填了隐式默认值。

线上实测（升级前的真实状态，216 行 / id 7055..7270 / 2026-07-02~07-24）：
  - `dedupe_key` 全为 ''（216/216）
  - `incident_no` 全为 0（216/216）
  - `window_start_at` 全为零值日期 '0000-00-00 00:00:00'（216/216，SQLAlchemy
    读回这类值会抛解析错误，属潜在炸弹）
  - `uq_alert_events_dedupe_incident` 唯一索引**在库里不存在**
  → 「30 分钟窗口去重」从上线至今从未生效。

代码侧修复已合入（`_fire_event` 计算三列 + flush 不再被吞异常打翻）；本修订负责
把存量数据补成可加唯一索引的形态。

实现要点
========
- 三步回填全部条件化（只改坏数据），可重复执行：
  1. `dedupe_key` 为空 → `'legacy-' || id`（逐行唯一，天然不撞约束）；
  2. `incident_no` 为 0/NULL → 1；
  3. `window_start_at` 为 NULL 或零值日期 → 取 `created_at`（实测非空）。
- 方言分支：字符串拼接 MySQL 用 `CONCAT()`、SQLite 用 `||`；标识符分别用反引号/
  双引号（同 0060/0061 的处理）。
- 唯一索引 inspector 幂等创建；全新链（`create_all` 已由模型建出）自动跳过。
- **不动 `status`**：存量 216 行 `status=''` 同样违反 `ck_alert_events_status`，
  但把它改成 ACTIVE 会让 216 条历史告警突然出现在告警中心，改成 RESOLVED 又是
  替用户判定「这些告警已经恢复」——属数据语义决策，本修订不做，留待拍板。
- downgrade 只回滚索引，不撤销回填（回填是把非法值改成合法值，回退无意义且会
  再次制造 216 行同键脏数据）。

DoD 证据
========
- 升级前后对 `alert_events` 的列值分布探针（见报告 §七.9）；
- `tests/test_whitebox_alerts.py` 的 3 条 PT-DEF-9 守护用例（三列必填、窗口去重、
  多标的互不压制）。
"""
from __future__ import annotations

from alembic import op

revision = "wps_0023_062_alert_events_dedupe_backfill"
down_revision = "wps_0023_061_snapshot_members_json_mediumtext"
branch_labels = None
depends_on = None

_TABLE = "alert_events"
_UNIQUE_INDEX = "uq_alert_events_dedupe_incident"
_UNIQUE_COLS = ["dedupe_key", "incident_no"]


def _table_exists(conn) -> bool:
    from sqlalchemy import inspect as sa_inspect

    return _TABLE in sa_inspect(conn).get_table_names()


def _existing_index_names(conn) -> set:
    from sqlalchemy import inspect as sa_inspect

    if not _table_exists(conn):
        return set()
    return {i["name"] for i in sa_inspect(conn).get_indexes(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return

    is_sqlite = conn.dialect.name == "sqlite"
    q = '"' if is_sqlite else "`"
    t = f"{q}{_TABLE}{q}"
    # 字符串拼接：SQLite 用 || ，MySQL 用 CONCAT()
    legacy_key = f"'legacy-' || {q}id{q}" if is_sqlite else f"CONCAT('legacy-', {q}id{q})"

    # 1) dedupe_key 空值 → 逐行唯一的 legacy-<id>
    op.execute(
        f"UPDATE {t} SET {q}dedupe_key{q} = {legacy_key} "
        f"WHERE {q}dedupe_key{q} IS NULL OR {q}dedupe_key{q} = ''"
    )
    # 2) incident_no 缺失（非严格模式下的隐式 0）→ 首次事件
    op.execute(
        f"UPDATE {t} SET {q}incident_no{q} = 1 "
        f"WHERE {q}incident_no{q} IS NULL OR {q}incident_no{q} = 0"
    )
    # 3) window_start_at 的 NULL / 零值日期 → 用 created_at 兜底（去重窗口起点）
    op.execute(
        f"UPDATE {t} SET {q}window_start_at{q} = {q}created_at{q} "
        f"WHERE {q}window_start_at{q} IS NULL "
        f"OR {q}window_start_at{q} <= '1970-01-01 00:00:00'"
    )

    # 4) 回填后 (dedupe_key, incident_no) 已逐行唯一，补建模型声明的唯一索引
    if _UNIQUE_INDEX not in _existing_index_names(conn):
        op.create_index(_UNIQUE_INDEX, _TABLE, _UNIQUE_COLS, unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    # 只回滚索引；回填后的合法值不撤销（见模块 docstring）
    if _UNIQUE_INDEX in _existing_index_names(conn):
        op.drop_index(_UNIQUE_INDEX, table_name=_TABLE)
