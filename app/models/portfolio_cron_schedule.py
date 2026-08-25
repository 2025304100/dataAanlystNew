"""组合级调度时间表（默认20:30正式auto_simulation，hour范围仅允许20-23应用层拦截）。"""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import DateTime, Integer, String, ForeignKey, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
class PortfolioCronSchedule(Base):
    __tablename__ = "portfolio_cron_schedules"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "schedule_type", name="uq_portfolio_cron_sched_portfolio_type"),
        CheckConstraint("schedule_type IN ('auto_simulation','daily_preview_dry_run')", name="ck_portfolio_cron_sched_type"),
        CheckConstraint("schedule_hour BETWEEN 0 AND 23", name="ck_portfolio_cron_sched_hour"),
        CheckConstraint("schedule_minute BETWEEN 0 AND 59", name="ck_portfolio_cron_sched_minute"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False, index=True)
    schedule_type: Mapped[str] = mapped_column(String(32), nullable=False, default="auto_simulation", server_default="auto_simulation")
    schedule_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=20, server_default="20", index=True, comment="正式链路仅允许20-23；预览dry_run默认15（应用层SCHEDULE_TOO_EARLY针对auto_simulation类型）")
    schedule_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近一次触发UTC naive时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
