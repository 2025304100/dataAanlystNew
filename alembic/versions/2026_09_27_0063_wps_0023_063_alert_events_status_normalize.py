"""PT-DEF-9 收尾：规范化 alert_events 的 status / severity_level 非法空值。

Revision ID: wps_0023_063_alert_events_status_normalize
Revises: wps_0023_062_alert_events_dedupe_backfill

为什么需要（2026-09-27，体检报告 §七.9 / §八 的遗留项）
==========================================================
`models/alert.py` 声明了两条 CHECK：

  - `ck_alert_events_status`：status ∈ (ACTIVE, ACKNOWLEDGED, RESOLVED, SUPPRESSED)
  - `ck_alert_events_severity_level`：severity_level ∈ (L3, L2, L1)

两列都是 NOT NULL 且无 server_default，但 WP-MSG.1 之前的写入口从不填它们。
线上（MySQL 5.7.26 忽略 CHECK）实测 216 行 `status=''` + `severity_level=''`，
即**全部违反自己声明的约束**。这批脏数据有两个现实危害：

  1. 同一张表若在 MySQL 8.x（真正执行 CHECK）或 SQLite（测试/dev 库）上重建，
     旧行会直接卡住 CHECK；
  2. `''` 不属于任何状态 → 按 status 过滤的查询（告警中心、未确认计数）
     行为不确定，同一批数据在不同入口时隐时现。

如何做到“不替用户改判”（关键）
================================
探针实测：这 216 行 **全部 `acknowledged=1`**、`resolved_at IS NULL`。
`acknowledged=1` 本身就是“已被用户确认”的事实，因此：

  - `acknowledged=1` → `status='ACKNOWLEDGED'`：如实映射，**不会**让历史告警
    重新出现在未读/活动列表里（这正是之前把 '' 改成 ACTIVE 方案被否掉的原因）；
  - `acknowledged=0` → `status='ACTIVE'`：这类行本来就是“未确认”的活动告警，
    此前因 status='' 被过滤掉属于隐性丢失，规范化后恢复可见是本意；
  - 已是合法值的行一律不动。

`severity_level` 按既有 `severity` 列推导（error→L1、warn→L2、info→L3、
其余→L2，与模型的 L2 默认一致），不凭空造等级。

实现要点
========
- 两条 UPDATE 全部条件化（只改非法值），幂等可重跑；
- 只改数据，不动约束本身（SQLite 的 CHECK 由模型在 build 时生成，MySQL 5.7
  不执行 CHECK，因此本修订对两库都是“修数”而非“改结构”）；
- downgrade 不撤销（把合法值改回空串没有意义）。

DoD 证据
========
- 执行前后对 `status/severity_level` 分组计数探针（见报告 §八.1 / §十）。
"""
from __future__ import annotations

from alembic import op

revision = "wps_0023_063_alert_events_status_normalize"
down_revision = "wps_0023_062_alert_events_dedupe_backfill"
branch_labels = None
depends_on = None

_TABLE = "alert_events"

_VALID_STATUS = ("'ACTIVE'", "'ACKNOWLEDGED'", "'RESOLVED'", "'SUPPRESSED'")


def _table_exists(conn) -> bool:
    from sqlalchemy import inspect as sa_inspect

    return _TABLE in sa_inspect(conn).get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return

    is_sqlite = conn.dialect.name == "sqlite"
    q = '"' if is_sqlite else "`"
    t = f"{q}{_TABLE}{q}"
    status = f"{q}status{q}"
    sev = f"{q}severity{q}"
    sev_level = f"{q}severity_level{q}"
    ack = f"{q}acknowledged{q}"

    # 1) status 非法/空 → 按 acknowledged 如实映射（已确认=ACKNOWLEDGED，未确认=ACTIVE）
    op.execute(
        f"UPDATE {t} SET {status} = CASE WHEN {ack} = 1 "
        f"THEN 'ACKNOWLEDGED' ELSE 'ACTIVE' END "
        f"WHERE {status} IS NULL OR {status} NOT IN ({', '.join(_VALID_STATUS)})"
    )

    # 2) severity_level 非法/空 → 由既有 severity 推导，不凭空造等级
    op.execute(
        f"UPDATE {t} SET {sev_level} = CASE "
        f"WHEN LOWER({sev}) = 'error' THEN 'L1' "
        f"WHEN LOWER({sev}) = 'info' THEN 'L3' "
        f"ELSE 'L2' END "
        f"WHERE {sev_level} IS NULL OR {sev_level} NOT IN ('L1', 'L2', 'L3')"
    )


def downgrade() -> None:
    # 不撤销：把合法状态改回 '' 只会重新制造违反 CHECK 的脏数据。
    pass
