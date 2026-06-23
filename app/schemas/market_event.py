from datetime import datetime

from pydantic import BaseModel, Field

from app.models.market_event import IMPACT_SCOPE_CHOICES


class MarketEventCreate(BaseModel):
    title: str
    summary: str | None = None
    impact_scope: str = Field(default="other")
    importance_level: int = Field(default=1, ge=1, le=5)
    affected_market: str = Field(default="A股")
    affected_sectors: str | None = None
    affected_symbols: str | None = None
    sentiment: str = Field(default="neutral")
    source_url: str | None = None
    published_at: datetime | None = None


class MarketEventUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    impact_scope: str | None = None
    importance_level: int | None = Field(default=None, ge=1, le=5)
    affected_market: str | None = None
    affected_sectors: str | None = None
    affected_symbols: str | None = None
    sentiment: str | None = None
    source_url: str | None = None
    published_at: datetime | None = None


class MarketEventRead(BaseModel):
    id: int
    title: str
    summary: str | None = None
    impact_scope: str
    importance_level: int
    affected_market: str
    affected_sectors: str | None = None
    affected_symbols: str | None = None
    sentiment: str
    source: str
    source_url: str | None = None
    is_manual: int
    published_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MarketEventCollectRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=30)
    sources: list[str] = Field(default_factory=lambda: ["cctv", "baidu", "eastmoney"])


class MarketEventListResponse(BaseModel):
    events: list[MarketEventRead]
    total: int
    by_scope: dict[str, int] = Field(default_factory=dict)
    by_level: dict[str, int] = Field(default_factory=dict)


class MarketEventCollectResponse(BaseModel):
    collected: int
    skipped_duplicate: int
    errors: list[str] = Field(default_factory=list)