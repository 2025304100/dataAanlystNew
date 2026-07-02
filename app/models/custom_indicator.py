from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CustomIndicator(Base):
    __tablename__ = "custom_indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    key: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="custom", index=True)
    formula: Mapped[str] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(16), default="boolean")
    params_json: Mapped[str] = mapped_column(Text, default="[]")
    scope_json: Mapped[str] = mapped_column(Text, default="[\"backtest\", \"discovery\"]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class CustomIndicatorVersion(Base):
    __tablename__ = "custom_indicator_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    indicator_id: Mapped[int] = mapped_column(ForeignKey("custom_indicators.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    formula: Mapped[str] = mapped_column(Text)
    params_json: Mapped[str] = mapped_column(Text, default="[]")
    value_type: Mapped[str] = mapped_column(String(16), default="boolean")
    change_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
