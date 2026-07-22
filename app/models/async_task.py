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
