from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DiscoveryCandidate(Base):
    """挖掘结果独立存储表（与业务表 symbols 物理隔离）。

    挖掘评分结果写入此表，不写入 symbols 表。
    用户手动点击"加入候选池"才执行 promote_candidate 晋升到业务表。
    """

    __tablename__ = "discovery_candidates"
    __table_args__ = (
        UniqueConstraint("scan_run_id", "universe_symbol_id", name="uq_candidate_run_symbol"),
        Index("ix_candidate_promoted", "is_promoted", "created_at"),
        Index("ix_candidate_run", "scan_run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"))
    universe_symbol_id: Mapped[int] = mapped_column(ForeignKey("universe_symbols.id", ondelete="CASCADE"))
    # 冗余字段（前端展示用，避免 join）
    symbol: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 评分快照
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    timing_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    dimension_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    scoring_config_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage: Mapped[str | None] = mapped_column(String(16), nullable=True)  # hold/reduce/observe
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)  # buy/watch/...
    reason_tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_days: Mapped[int] = mapped_column(Integer, default=3)
    valid_days: Mapped[int] = mapped_column(Integer, default=5)
    # 晋升状态（核心：默认 0，手动晋升后变 1）
    is_promoted: Mapped[int] = mapped_column(Integer, default=0)  # 0=未晋升 1=已加入候选池
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    # 关联基础表 universe_symbols（读取 market/region 等，不依赖 symbols 表）
    universe_symbol_ref = relationship("UniverseSymbol", lazy="joined")
