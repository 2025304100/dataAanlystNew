from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DiscoveryTaskRecord(Base):
    __tablename__ = "discovery_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    stage: Mapped[str] = mapped_column(String(16), index=True)
    percent: Mapped[float] = mapped_column(Float, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(32), index=True)
    min_score: Mapped[float] = mapped_column(Float, default=55)
    include_news: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    ok_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    empty_count: Mapped[int] = mapped_column(Integer, default=0)
    scored_count: Mapped[int] = mapped_column(Integer, default=0)
    batch_size: Mapped[int] = mapped_column(Integer, default=20)
    delay_seconds: Mapped[float] = mapped_column(Float, default=0.25)
    adaptive_delay_seconds: Mapped[float] = mapped_column(Float, default=0.25)
    symbol_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scan_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    executable_count: Mapped[int] = mapped_column(Integer, default=0)
    cleanup_count: Mapped[int] = mapped_column(Integer, default=0)
    news_symbols_total: Mapped[int] = mapped_column(Integer, default=0)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_symbol_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_symbol_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    # ── WP-P.1：挖掘性能监控字段（全部 nullable，向后兼容旧库） ──
    # 评分快照关联（WP-P.2 引入）
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    snapshot_hit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0/1
    # 阶段耗时（毫秒，JSON 数组：[{stage, started_at, finished_at, duration_ms}, ...]）
    stage_durations_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 复用与失效统计
    dirty_symbol_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reused_score_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rescored_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coarse_match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    advanced_match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_rows_written: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 扫描缓存（WP-P.6 使用）
    cache_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    cache_hit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0/1
    # 降级原因
    degraded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
