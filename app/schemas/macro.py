from datetime import datetime

from pydantic import BaseModel, Field


class MacroUpdateRequest(BaseModel):
    region: str = Field(default="all", pattern="^(all|cn|us)$")


class MacroIndicatorRead(BaseModel):
    id: int | None = None
    region: str
    category: str
    indicator_key: str
    name: str
    period: str
    value: float | None = None
    previous_value: float | None = None
    delta: float | None = None
    unit: str | None = None
    frequency: str
    source: str
    score: float
    status: str
    updated_at: datetime | None = None


class MacroSnapshotRead(BaseModel):
    id: int | None = None
    region: str
    market_score: float
    stance: str
    summary: str
    growth_score: float
    inflation_score: float
    liquidity_score: float
    credit_score: float
    risk_score: float
    indicators_total: int
    failed_total: int
    created_at: datetime | None = None


class MacroOverviewResponse(BaseModel):
    region: str
    snapshot: MacroSnapshotRead | None = None
    indicators: list[MacroIndicatorRead] = Field(default_factory=list)
    brief: list[str] = Field(default_factory=list)
    failed: list[dict] = Field(default_factory=list)
