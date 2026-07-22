"""外部接口运行时状态模型（WP-S.2 熔断器）。

记录每个第三方接口（interface_key）的：
- 熔断器状态机：closed / open / half_open
- 失败计数与冷却到期时间
- 请求计数、缓存命中、降级回退次数（可观测性）

设计要点：
- 与 AkshareApiConfig 区分：后者管理用户可配置的接口策略（延时/重试/启用），
  本表只承载熔断器状态机的运行时记录，由网关自动更新。
- interface_key 全局唯一（如 "akshare.daily_bars" / "akshare.index_prices"）。
- 与 init_db.py 的 schema patch 配合，支持 SQLite/MySQL 双库升级。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# 熔断器状态枚举（避免散落字符串字面量）
STATE_CLOSED = "closed"          # 正常放行
STATE_OPEN = "open"              # 熔断中，拒绝请求
STATE_HALF_OPEN = "half_open"    # 冷却到期，允许 1 个探测


class ExternalEndpointRuntime(Base):
    """外部接口运行时状态（熔断器 + 计数器）。

    字段说明：
        interface_key: 接口唯一标识，对应网关 fetch() 的 interface_key 参数
        host: 主机名（用于主机级限流统计，如 "push2his.eastmoney.com"）
        state: 熔断器状态 closed/open/half_open
        consecutive_failures: 连续失败次数（成功后归零）
        cooldown_until: 熔断到期时间（UTC naive），过期后转为 half_open
        last_error_code: 最后一次失败的错误码（HTTP 429/403/timeout/connection 等）
        last_error_at: 最后一次失败时间戳
        request_count: 累计真实请求数（不含缓存命中）
        cache_hit_count: 累计缓存命中数（L1/L2/L3 命中）
        fallback_count: 累计降级回退次数（L4 失败后回退到 L3/L2/L1）
        updated_at: 最后更新时间（每次状态变更或请求记录时刷新）
    """

    __tablename__ = "external_endpoint_runtime"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interface_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    host: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default=STATE_CLOSED, index=True)

    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    request_count: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit_count: Mapped[int] = mapped_column(Integer, default=0)
    fallback_count: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )


__all__ = [
    "ExternalEndpointRuntime",
    "STATE_CLOSED",
    "STATE_OPEN",
    "STATE_HALF_OPEN",
]
