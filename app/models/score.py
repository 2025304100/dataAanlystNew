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
    calc_batch_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    symbol_ref = relationship("Symbol", back_populates="scores")

