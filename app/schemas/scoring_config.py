from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ScoringConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    asset_type: str
    preset_key: str
    name: str
    description: str | None = None
    version: int
    config_json: str
    preset_source: str
    is_system: int
    is_active: int
    is_latest: int
    base_preset_key: str | None = None
    created_at: datetime
    updated_at: datetime


class ScoringConfigParsed(ScoringConfigRead):
    """带解析后 config 字段的预设（供前端编辑使用）。"""
    config: dict[str, Any] | None = None


class ScoringConfigCreate(BaseModel):
    asset_type: Literal["stock", "etf"]
    preset_key: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = None
    config: dict[str, Any]
    base_preset_key: str | None = None


class ScoringConfigUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    description: str | None = None
    config: dict[str, Any] | None = None


class ScoringConfigDuplicate(BaseModel):
    new_preset_key: str = Field(..., min_length=1, max_length=64)
    new_name: str = Field(..., min_length=1, max_length=128)
    new_description: str | None = None


class ScoringConfigVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    asset_type: str
    preset_key: str
    name: str
    version: int
    config_json: str
    is_active: int
    is_latest: int
    created_at: datetime
