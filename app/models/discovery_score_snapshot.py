from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DiscoveryScoreSnapshot(Base):
    """评分快照主表（WP-P.2）。

    表示某 scope 在某交易日的一次评分快照构建任务。状态机：
    building → ready / failed；ready 后被新版本替换 → superseded。
    扫描永不读 building 状态的快照（防半成品）。
    """

    __tablename__ = "discovery_score_snapshots"
    __table_args__ = (
        # (scope, status, trade_date, generated_at) 用于查找某 scope 最新 ready 快照
        Index(
            "ix_snapshot_scope_status_date",
            "scope",
            "status",
            "trade_date",
            "generated_at",
        ),
        # (scope, status, trade_date) 同一 scope 同一交易日同时只允许一个 building
        UniqueConstraint(
            "scope", "status", "trade_date", name="uq_snapshot_scope_status_date"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), index=True, nullable=False)  # cn_stock / cn_etf / us_stock
    trade_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    # 状态：building / ready / failed / superseded
    status: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, default="building"
    )

    # 评分配置与因子模型快照
    scoring_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    scoring_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # manual / shadow / ridge
    factor_model_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_cutoff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 构建元数据
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    build_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # 覆盖率与失效统计
    symbol_count: Mapped[int] = mapped_column(Integer, default=0)
    coverage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    dirty_symbol_count: Mapped[int] = mapped_column(Integer, default=0)

    # 错误摘要
    error_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )


class DiscoveryScoreSnapshotItem(Base):
    """评分快照明细表（WP-P.2）。

    每个 item 对应一个 universe_symbol 在该快照下的评分明细。
    manifest（配置/模型/数据截止）存在 snapshot 主表，不在每个 item 重复。
    """

    __tablename__ = "discovery_score_snapshot_items"
    __table_args__ = (
        # 优先查询：按 priority_score 倒序取 Top-K
        Index("ix_snapshot_item_priority", "snapshot_id", "priority_score"),
        # 按 action/stage 过滤
        Index(
            "ix_snapshot_item_action_stage",
            "snapshot_id",
            "action",
            "stage",
            "priority_score",
        ),
        # 唯一约束：同一快照下同一 universe_symbol 只能有一条
        UniqueConstraint(
            "snapshot_id", "universe_symbol_id", name="uq_snapshot_item_symbol"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("discovery_score_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    universe_symbol_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    symbol_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # 三类分数
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    timing_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 维度分数 JSON（如 {"breakout": 80, "pullback": 75, ...}）
    dimension_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 阶段与动作（来自评分逻辑）
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 数据可信度与健康摘要
    data_credibility: Mapped[float | None] = mapped_column(Float, nullable=True)
    health_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
