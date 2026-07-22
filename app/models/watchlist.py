from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Watchlist(Base):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    list_type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    items = relationship("WatchlistItem", back_populates="watchlist_ref", cascade="all, delete-orphan")


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("watchlist_id", "symbol_id", name="uq_watchlist_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("watchlists.id", ondelete="CASCADE"))
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # WP-P.7：候选晋升为观察项时复制的入选评分快照（JSON）
    # 不引用快照 item ID（防止快照清理后引用断裂），仅保存快照评分副本
    score_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # WP2.1：正式观察池扩展字段
    # 来源类型：manual/candidate/scan_result/alert/legacy_manual_unknown
    origin_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual",
        comment="来源类型：manual/candidate/scan_result/alert/legacy_manual_unknown",
    )
    # 来源对象 ID（如 discovery_candidate.id / scan_result.id / alert.id）
    origin_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True, comment="来源对象 ID")
    # 加入原因 JSON（候选快照、扫描上下文、告警上下文等）
    reason_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加入原因 JSON")
    # 状态：watching/ready/invalid/archived
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="watching",
        comment="状态：watching/ready/invalid/archived",
    )
    # 优先级（数值越大越优先）
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="优先级，数值越大越优先",
    )
    # 标签 JSON 数组（如 ["科技", "龙头"]）
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="标签 JSON 数组")
    # 目标组合 ID（可选，关联 portfolios 表）
    target_portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="SET NULL"), nullable=True, index=True,
        comment="目标组合 ID",
    )
    # 最后更新时间（修改任意字段时刷新）
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=lambda: datetime.now(timezone.utc),
        comment="最后更新时间",
    )
    # 归档时间（status 转为 archived 时写入）
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="归档时间")

    watchlist_ref = relationship("Watchlist", back_populates="items")

