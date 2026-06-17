from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ScanPreset(Base):
    __tablename__ = "scan_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    scope_type: Mapped[str] = mapped_column(String(16))
    markets: Mapped[str | None] = mapped_column(Text, nullable=True)
    boards: Mapped[str | None] = mapped_column(Text, nullable=True)
    filters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preset_id: Mapped[int | None] = mapped_column(ForeignKey("scan_presets.id", ondelete="SET NULL"), nullable=True)
    run_name: Mapped[str] = mapped_column(String(128))
    scope_snapshot: Mapped[str] = mapped_column(Text)
    filters_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    portfolio_id: Mapped[int | None] = mapped_column(ForeignKey("portfolios.id", ondelete="SET NULL"), nullable=True)
    portfolio_rule_id: Mapped[int | None] = mapped_column(ForeignKey("portfolio_rules.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    results = relationship("ScanResult", back_populates="scan_run_ref", cascade="all, delete-orphan")


class ScanResult(Base):
    __tablename__ = "scan_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    result_type: Mapped[str] = mapped_column(String(16), index=True)
    rank_no: Mapped[int] = mapped_column(Integer)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    timing_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recommended_position_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_sector_overweight: Mapped[int] = mapped_column(Integer, default=0)
    is_asset_overweight: Mapped[int] = mapped_column(Integer, default=0)
    reason_tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_days: Mapped[int] = mapped_column(Integer, default=3)
    valid_days: Mapped[int] = mapped_column(Integer, default=5)
    is_frozen: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    scan_run_ref = relationship("ScanRun", back_populates="results")
