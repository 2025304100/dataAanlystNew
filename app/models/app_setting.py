"""通用应用设置表 `app_settings`（键 → JSON 值）。

为什么要单独一张表，而不是复用因子域的 `factor_system_config.extra_json`
================================================================
`factor_system_config` 的 docstring 自陈是 "Persistent **feature readiness**
settings managed from the settings page"，属**因子域**。把发现中心（Discovery）
乃至其它域的设置塞进那张表，就是组合交易域体检报告反复指出并治理的那一类
"模块边界脱节 / 跨域耦合"——读写两侧都会出现"名为主然"的隐契约。
本表按点分命名空间（``"<domain>.<name>"``）承载各域的用户级设置：
新增一个开关只是插一行，不需要新表、也不需要改表结构。

约定
====
- 主键就是业务键 ``key``（无自增 id）：读路径永远是 ``db.get(AppSetting, key)``，
  不需要额外索引。
- 值统一按 JSON 存（标量也照 JSON：``true`` / ``0.7`` / ``"ridge"``），
  避免再做"这列是布尔还是数字"的类型分派。
- ``updated_by`` 记录变更来源（``settings-page:<user>`` / ``seed`` / ``api``），
  便于事后回答"谁把这个开关关了"。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AppSetting(Base):
    """按点分命名空间存放的应用级设置（由设置页/HTTP 端点写入）。"""

    __tablename__ = "app_settings"

    # 形如 "discovery.auto_data_prep_enabled"；主键即业务键
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # JSON 文本。缺省 "null" 而不是 "{}"，让"显式置 null"与"未设置"可区分
    # （未设置 = 无行；读到 null 视为已设置为 null）。
    value_json: Mapped[str] = mapped_column(Text, default="null", nullable=False)
    updated_by: Mapped[str] = mapped_column(
        String(128), default="system", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive, nullable=False
    )
