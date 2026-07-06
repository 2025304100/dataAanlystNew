from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AkshareApiConfig(Base):
    """第三方 akshare 接口配置与运行时状态。

    - api_key：接口标识（与 akshare_registry 中的 key 一致，如 stock_zh_a_spot_em）
    - enabled：是否启用（禁用时调用方应跳过该数据源）
    - anti_risk_strategy：防风控策略档位 fast/standard/conservative/extreme/custom
    - delay_min_ms/delay_max_ms：自定义延时区间（毫秒），仅 strategy=custom 时使用
    - 运行时状态字段（last_probe_*、last_call_*、total_*）由系统更新，用户只读

    设计：接口元数据（名称/分类/默认推荐档位）放代码 registry 中不可修改；
         用户可配置的只有 enabled、anti_risk_strategy、delay_min/max_ms（custom 时）。
    """

    __tablename__ = "akshare_api_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    anti_risk_strategy: Mapped[str] = mapped_column(String(16), default="standard")
    # 自定义延时（仅 strategy=custom 生效）
    delay_min_ms: Mapped[int] = mapped_column(Integer, default=300)
    delay_max_ms: Mapped[int] = mapped_column(Integer, default=800)

    # 运行时状态（只读，由 probe / 实际调用更新）
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_probe_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_probe_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_probe_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_call_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_call_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_call_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_failures: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
