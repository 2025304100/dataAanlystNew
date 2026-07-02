from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AlertRule(Base):
    """告警规则：定义什么条件触发告警。"""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    alert_type: Mapped[str] = mapped_column(String(32), index=True)
    # score_drop / data_stale / indicator_trigger / task_failed / watchlist_signal
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    severity: Mapped[str] = mapped_column(String(16), default="warn")  # info / warn / error
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 存储阈值等参数，如 {"threshold": 40, "stale_days": 7, "symbol_ids": [...]}
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )


class AlertEvent(Base):
    """告警事件：规则触发后产生的记录。"""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    alert_type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warn")
    title: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    symbol_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        index=True,
    )
