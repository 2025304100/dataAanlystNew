from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ScoringConfig(Base):
    """评分配置预设。

    存储股票/ETF 的系统预设、用户预设、当前激活状态和历史版本。
    同一 asset_type 同一时间只能有一个 is_active=1 的预设；
    同一 asset_type + preset_key 只能有一个 is_latest=1 的版本。
    """

    __tablename__ = "scoring_configs"
    __table_args__ = (
        UniqueConstraint("asset_type", "preset_key", "version", name="uq_scoring_config_type_key_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_type: Mapped[str] = mapped_column(String(16), index=True)
    preset_key: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    config_json: Mapped[str] = mapped_column(Text)
    preset_source: Mapped[str] = mapped_column(String(16), default="system")  # system / user
    is_system: Mapped[int] = mapped_column(Integer, default=0, index=True)
    is_active: Mapped[int] = mapped_column(Integer, default=0, index=True)
    is_latest: Mapped[int] = mapped_column(Integer, default=1, index=True)
    base_preset_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
