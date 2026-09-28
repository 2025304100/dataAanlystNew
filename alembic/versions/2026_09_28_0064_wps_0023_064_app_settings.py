"""通用应用设置表 `app_settings`（PT-DEF-15 设置升级：用户级持久化开关）。

Revision ID: wps_0023_064_app_settings
Revises: wps_0023_063_alert_events_status_normalize

为什么需要
==========
发现中心的「无 ready 快照 → 自动启动数据准备」原先只有**运维级 env 开关**
`DISCOVERY_AUTO_DATA_PREP_ENABLED`。要升级成用户可在设置页持久化的开关，
本仓并没有通用配置表：最接近的 `factor_system_config.extra_json` 属**因子域**
（其 docstring 自陈 "feature readiness settings"），把发现中心的设置塞进去就是
体检报告反复治理的那一类「跨域耦合 / 模块边界脱节」。

所以新建一张按点分命名空间存 JSON 值的通用表，各域设置都往这里放：
新增一个开关只是插一行，不需要新表、也不需要再改表结构。

生效优先级（服务层 `app.services.app_settings.resolve_bool_setting`）：
**表（用户改的）> 环境变量（运维兜底）> 代码默认值**。
本迁移只建表、不 seed 任何行 —— 没有行就等于「用户没改过」，让 env / 默认值
继续生效，因此本修订对现有行为零影响（回滚也是干净的 DROP TABLE）。

实现要点
========
- inspector 判存在，幂等可重跑（本仓迁移一律要求可重复执行，G0 约定）；
- 主键即业务键 `key`，不再加自增 id（读路径永远是 `db.get(AppSetting, key)`）；
- **`value_json` 不能给 server_default**：MySQL 对 BLOB/TEXT 列默认值直接报
  `1101 BLOB, TEXT, GEOMETRY or JSON column ... can't have a default value`
  （线上实测）。缺省值由 ORM 的 python-side default="null" 负责；手工插行时
  必须显式写上值（下面 0 行的设计使其无需兼容手工默认）。
- `updated_at` 给 `server_default=now()`，让手工插行（seed / 运维救火）也有时间戳。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "wps_0023_064_app_settings"
down_revision = "wps_0023_063_alert_events_status_normalize"
branch_labels = None
depends_on = None

_TABLE = "app_settings"


def _table_exists(conn) -> bool:
    return _TABLE in sa.inspect(conn).get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn):
        return

    op.create_table(
        _TABLE,
        sa.Column("key", sa.String(length=128), nullable=False),
        # 无 server_default：MySQL 不允许 TEXT 列带默认值（实测 1101）
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False,
                  server_default="system"),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key", name=f"pk_{_TABLE}"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    op.drop_table(_TABLE)
