from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryPlanFilter(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    indicator_key: str | None = Field(default=None, max_length=96)
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq"] = "eq"
    number_value: float = 0
    boolean_value: bool = True


class DiscoveryPlanBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    logic: Literal["AND", "OR"] = "AND"
    pool_tab: Literal["all", "highQuality", "highTiming", "actionable", "overheatRisk", "lowCredibility"] = "actionable"
    filters: list[DiscoveryPlanFilter] = Field(default_factory=list)


class DiscoveryPlanCreate(DiscoveryPlanBase):
    pass


class DiscoveryPlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    logic: Literal["AND", "OR"] | None = None
    pool_tab: Literal["all", "highQuality", "highTiming", "actionable", "overheatRisk", "lowCredibility"] | None = None
    filters: list[DiscoveryPlanFilter] | None = None


class DiscoveryPlanRead(DiscoveryPlanBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
