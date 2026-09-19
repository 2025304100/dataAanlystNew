from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (UniqueConstraint("symbol_id", "trade_date", "calc_batch_id", name="uq_score_symbol_date_batch"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    quality_score: Mapped[float] = mapped_column(Float)
    quality_grade: Mapped[str] = mapped_column(String(1))
    timing_score: Mapped[float] = mapped_column(Float)
    stage: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(16), index=True)
    priority_score: Mapped[float] = mapped_column(Float)
    # 股质评分分项
    trend_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    momentum_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    breadth_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 时点评分分项（新增）
    breakout_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pullback_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    overheat_penalty: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 数据可信度（P0-4.3）
    data_credibility: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 评分配置快照（P0：轻量自定义评分配置）
    scoring_asset_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    scoring_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    scoring_preset_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scoring_preset_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scoring_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scoring_config_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    dimension_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    factor_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 动态因子模型快照；默认 manual，不影响现有评分链路
    weight_mode: Mapped[str] = mapped_column(
        String(16), default="manual", index=True
    )
    factor_model_run_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    factor_data_cutoff_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # WP7-05: Score 解释追溯字段
    # factor_set_id: 该 Score 对应的 FactorSet ID（manual 模式为 None）
    factor_set_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # factor_member_versions_json: 成员版本快照
    # {"factor_code": {"version": 1, "role": "feature", "missing_policy": "exclude"}}
    factor_member_versions_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    factor_quality_score: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    factor_timing_score: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    model_alpha_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    macro_regime: Mapped[str | None] = mapped_column(
        String(24), nullable=True
    )
    macro_position_multiplier: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    calc_batch_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # -- G0-WP0-2b / Q4 PIT: disclosure timestamps --
    # WP0-6 TR-06.5 升级：published_at NOT NULL（migration 033 已对历史行回填 created_at）
    published_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True,
        default=lambda: datetime.now(timezone.utc),
        comment="First publication datetime (UTC naive DB). Backfill = created_at if missing.",
    )
    first_published_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="First-ever publish of this fact (audit)",
    )
    revision_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="Latest revision time of this fact; published_at may be backdated",
    )
    pit_flag: Mapped[str] = mapped_column(
        String(16), default="NOT_CHECKED", server_default="NOT_CHECKED",
        comment="(Legacy) PIT_SAFE|NOT_PIT_SAFE|NOT_CHECKED. Kept for downgrade compatibility.",
    )
    # WP0-6 TR-06.5：正式 PIT 状态列（migration 033 新增；生产链路查询 WHERE pit_safety='PIT_VERIFIED'）
    pit_safety: Mapped[str] = mapped_column(
        String(16), default="NOT_PIT_SAFE", server_default="NOT_PIT_SAFE", index=True,
        comment="NOT_PIT_SAFE | PIT_VERIFIED. Set explicitly by data pipeline after publish.",
    )

    symbol_ref = relationship("Symbol", back_populates="scores")
