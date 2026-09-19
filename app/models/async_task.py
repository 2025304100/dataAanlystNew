from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AsyncTaskRecord(Base):
    """通用异步任务记录，支持多种任务类型（market_data_sync 等）。"""

    __tablename__ = "async_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid4.hex
    task_type: Mapped[str] = mapped_column(String(32), index=True)  # "market_data_sync" | ...
    status: Mapped[str] = mapped_column(String(16), index=True)     # queued|running|done|failed|cancelled
    stage: Mapped[str] = mapped_column(String(32), index=True)      # prepare|sync|score|scan|done
    percent: Mapped[float] = mapped_column(Float, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    ok_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    current_item: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    # WP-S.5 任务防卡死状态机扩展字段（全部 nullable，向后兼容旧数据）
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stage_budget_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_progress_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_step_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_recovery_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_patrol_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_thread_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancel_requested: Mapped[bool | None] = mapped_column(Integer, nullable=True, default=0)
    # FR-P1-2 可靠性扩展字段（与 alembic wps_0023_028 同步）
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True,
        comment="同事务去重键：task_type+portfolio_id+date+payload_hash+c id 生成；可用于 AC-10 重复投递 3 次零重复订单/成交",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="跨 request/async_task/decision_run/outbox/audit 统一关联 ID；8 hex，同 main.py 异常处理器",
    )
    is_terminal_locked: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=0,
        comment="终态锁定：status 进入 done/failed/cancelled 后置 1；之后任何 status/stage 改必须 WHERE !=1（project_memory #8 终态不得被 worker 线程覆盖）",
    )
    cancelled_timeout_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="CANCEL 请求超时时间点；若之后仍无终态，巡检升级为 cancelled_timeout 审计记录（不打断 status=cancelled 的终态语义）",
    )
