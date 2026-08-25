from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    __table_args__ = (
        UniqueConstraint("dedupe_key", "incident_no", name="uq_alert_events_dedupe_incident"),
        CheckConstraint("status IN ('ACTIVE','ACKNOWLEDGED','RESOLVED','SUPPRESSED')", name="ck_alert_events_status"),
        CheckConstraint("severity_level IN ('L3','L2','L1')", name="ck_alert_events_severity_level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    alert_type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warn")
    title: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    symbol_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[int] = mapped_column(Integer, default=0)
    portfolio_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True, comment="关联组合ID（可为空的系统级告警无此值）")
    decision_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="关联决策运行ID；决策前系统告警（数据源/调度失败）可为NULL")
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="关联异步任务ID或correlation_id；系统告警必填其一；决策告警可同时有decision_run_id")
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="关联追踪跨服务请求ID；任一非决策告警此ID与task_id至少一个非空")
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True, comment="30分钟窗口去重键=SHA256(portfolio_id:alert_code:severity:window_start)")
    incident_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="同一dedupe_key恢复后再次发生自增1；事务锁或重试保障并发不重复")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE", server_default="ACTIVE", index=True, comment="ACTIVE | ACKNOWLEDGED | RESOLVED | SUPPRESSED")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="告警恢复或手动关闭时间；用于解除去重窗口")
    window_start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="30分钟去重窗口起点UTC naive；用于dedupe_key一致性")
    severity_level: Mapped[str] = mapped_column(String(16), nullable=False, default="L2", server_default="L2", comment="告警等级 L3(INFO)/L2(WARN)/L1(CRITICAL)；默认L2；与message.severity可不一致（消息等级与事件等级解绑）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        index=True,
    )
