from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CustomIndicatorBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    description: str = ""
    category: str = Field(default="custom", max_length=32)
    formula: str = Field(min_length=1, max_length=500)
    value_type: Literal["boolean", "number"] = "boolean"
    params: list[dict[str, Any]] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=lambda: ["backtest", "discovery"])
    enabled: bool = True

    @field_validator("scope")
    @classmethod
    def normalize_scope(cls, value: list[str]) -> list[str]:
        allowed = {"backtest", "discovery", "alert", "review"}
        normalized = [item for item in dict.fromkeys(value or []) if item in allowed]
        return normalized or ["backtest"]


class CustomIndicatorCreate(CustomIndicatorBase):
    pass


class CustomIndicatorUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    key: str | None = Field(None, min_length=1, max_length=96, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    description: str | None = None
    category: str | None = Field(None, max_length=32)
    formula: str | None = Field(None, min_length=1, max_length=500)
    value_type: Literal["boolean", "number"] | None = None
    params: list[dict[str, Any]] | None = None
    scope: list[str] | None = None
    enabled: bool | None = None


class CustomIndicatorRead(CustomIndicatorBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    version: int = 1


class CustomIndicatorPreviewRequest(BaseModel):
    symbol_id: int = Field(gt=0)
    formula: str = Field(min_length=1, max_length=500)
    value_type: Literal["boolean", "number"] = "boolean"
    trade_date: date | None = None


class CustomIndicatorPreviewBar(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class CustomIndicatorPreviewScore(BaseModel):
    quality_score: float | None = None
    timing_score: float | None = None
    trend_score: float | None = None
    momentum_score: float | None = None


class CustomIndicatorPreviewRead(BaseModel):
    ok: bool = True
    message: str = "Preview ready"
    symbol_id: int
    symbol: str
    name: str
    trade_date: date
    value_type: Literal["boolean", "number"]
    result_boolean: bool | None = None
    result_number: float | None = None
    display_value: str
    latest_bar: CustomIndicatorPreviewBar
    score_snapshot: CustomIndicatorPreviewScore | None = None