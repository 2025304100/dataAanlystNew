"""Persistent, versioned subscriptions for market-sensitive events."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.mysql import LONGTEXT

from app.db.base import Base

LongText = Text().with_variant(LONGTEXT(), "mysql")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MarketSignalRule(Base):
    __tablename__ = "market_signal_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(16), index=True)
    target_key: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    config_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MarketSignalRuleVersion(Base):
    __tablename__ = "market_signal_rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "revision", name="uq_ms_rule_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("market_signal_rules.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    config_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MarketSignalEvidence(Base):
    __tablename__ = "market_signal_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(LongText)
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    time_quality: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(16), default="news")
    metadata_json: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MarketSignalSettings(Base):
    __tablename__ = "market_signal_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MarketSignalAssessment(Base):
    __tablename__ = "market_signal_assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("market_signal_evidence.id"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("market_signal_rules.id"), index=True)
    rule_revision: Mapped[int] = mapped_column(Integer)
    request_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    fact_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analysis_json: Mapped[str] = mapped_column(LongText)
    input_json: Mapped[str] = mapped_column(LongText)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MarketSignalPrediction(Base):
    __tablename__ = "market_signal_predictions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    fact_key: Mapped[str] = mapped_column(String(64))
    assessment_id: Mapped[int] = mapped_column(ForeignKey("market_signal_assessments.id"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("market_signal_rules.id"), index=True)
    rule_revision: Mapped[int] = mapped_column(Integer)
    target_type: Mapped[str] = mapped_column(String(16))
    target_key: Mapped[str] = mapped_column(String(64), index=True)
    delivery_status: Mapped[str] = mapped_column(String(24), index=True)
    outbox_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    snapshot_json: Mapped[str] = mapped_column(LongText)
    outcome_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    outcome_json: Mapped[str] = mapped_column(LongText, default="{}")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MarketSignalOutcomeVersion(Base):
    __tablename__ = "market_signal_outcomes"
    __table_args__ = (UniqueConstraint("prediction_id", "version", name="uq_ms_outcome_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("market_signal_predictions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    result_json: Mapped[str] = mapped_column(LongText)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
