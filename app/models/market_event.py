from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


IMPACT_SCOPE_CHOICES = [
    "macro_policy",
    "commodity_futures",
    "sector_dynamics",
    "international",
    "breaking",
    "fund_flow",
    "sentiment",
    "other",
]


class MarketEvent(Base):
    __tablename__ = "market_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact_scope: Mapped[str] = mapped_column(String(32), default="other", index=True)
    importance_level: Mapped[int] = mapped_column(Integer, default=1, index=True)
    affected_market: Mapped[str] = mapped_column(String(64), default="A\u80a1/\u671f\u8d27", index=True)
    affected_sectors: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL denotes historical labels with unknown origin; never guess that a
    # stored historical override was automatic. New writes explicitly set this.
    sector_tag_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    affected_symbols: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str] = mapped_column(String(16), default="neutral", index=True)
    source: Mapped[str] = mapped_column(String(64), default="manual", index=True)
    # P0-4：标题 MD5 落库（32 位十六进制），采集去重从"全表拉标题"改为索引查询。
    # 非唯一索引：存量数据允许历史重复标题，去重仍由应用层判断（与原行为一致）。
    title_hash: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_manual: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MarketEventCollectRun(Base):
    """P1-7：行情消息采集运行账目（可观测）。

    每次采集（手动/定时）落一行，供"最近采集"展示与连续空采告警判定；
    由采集流程顺带清理 90 天前的旧账目，不无限增长。
    """

    __tablename__ = "market_event_collect_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger_source: Mapped[str] = mapped_column(String(16), default="manual", index=True)  # manual / scheduled
    days: Mapped[int] = mapped_column(Integer, default=30)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    collected: Mapped[int] = mapped_column(Integer, default=0)
    skipped_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="done", index=True)  # done / partial / failed
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class SectorConceptConfig(Base):
    """P2-12：板块概念关键词的 DB 覆盖（运营化）。

    按 sector_id 覆盖内置关键词（整行替换；删除该行即恢复内置默认）。
    热点聚合与采集打标在运行时合并内置 + DB 覆盖，无需发版即可调词。
    """

    __tablename__ = "sector_concept_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sector_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    keywords: Mapped[str] = mapped_column(Text)  # 逗号分隔
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
