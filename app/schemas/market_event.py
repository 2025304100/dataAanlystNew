from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.core.market_event_policy import (
    MARKET_EVENT_KEEP_DAYS_DEFAULT, MARKET_EVENT_MIN_KEEP_DAYS, MARKET_EVENT_MAX_KEEP_DAYS,
)


class MarketEventCreate(BaseModel):
    title: str
    summary: str | None = None
    impact_scope: str = Field(default="other")
    importance_level: int = Field(default=1, ge=1, le=5)
    affected_market: str = Field(default="A\u80a1/\u671f\u8d27")
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
    sector_tag_source: Literal["auto", "manual"] | None = None


class MarketEventClassificationAnalysis(BaseModel):
    rule_version: str
    rule_fingerprint: str
    current_scope: str
    current_level: int
    current_sentiment: Literal["positive", "negative", "neutral"]
    scope_hits: dict[str,list[str]]
    level_hits: dict[str,list[str]]
    positive_hits: list[str]
    negative_hits: list[str]
    negated_phrases: list[str]
    scope_ties: list[str]
    matches_saved: bool


class MarketEventRead(BaseModel):
    id: int
    title: str
    summary: str | None = None
    impact_scope: str
    importance_level: int
    affected_market: str
    affected_sectors: str | None = None
    sector_tag_source: str = "legacy"
    affected_symbols: str | None = None
    sentiment: str
    classification_analysis: MarketEventClassificationAnalysis | None = None
    source: str
    source_url: str | None = None
    is_manual: int
    published_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MarketEventCollectRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=30)
    # P0-2：默认全源采集（此前只有 eastmoney-global+caixin 两个源会被默认采集）
    sources: list[str] = Field(
        default_factory=lambda: [
            "eastmoney-global", "caixin", "cctv", "baidu", "baidu-report", "futures-shmet",
        ]
    )


class MarketEventListResponse(BaseModel):
    events: list[MarketEventRead]
    total: int
    by_scope: dict[str, int] = Field(default_factory=dict)
    by_level: dict[str, int] = Field(default_factory=dict)
    expired_total: int = 0
    as_of: datetime | None = None
    snapshot_max_id: int | None = None


class MarketEventCollectResponse(BaseModel):
    collected: int
    skipped_duplicate: int
    errors: list[str] = Field(default_factory=list)
    status: Literal["done", "partial", "failed"] = "done"


class MarketEventCleanupRequest(BaseModel):
    """发布时间保留窗口；90～364 天的旧配置安全提升为 365 天。"""
    keep_days: int = Field(default=MARKET_EVENT_KEEP_DAYS_DEFAULT,
                           ge=MARKET_EVENT_MIN_KEEP_DAYS, le=MARKET_EVENT_MAX_KEEP_DAYS, strict=True)


class MarketEventCleanupResponse(BaseModel):
    deleted: int
    keep_days: int
    cutoff_date: datetime


class MarketEventHotspotSector(BaseModel):
    """P0-3：单个板块的热点统计。event_ids 为样本事件 id（上限截断）。"""
    sector_id: str
    bull: int
    bear: int
    neutral: int
    total: int
    max_level: int
    event_ids: list[int] = Field(default_factory=list)


class MarketEventHotspotsResponse(BaseModel):
    days: int
    generated_at: datetime
    sectors: list[MarketEventHotspotSector] = Field(default_factory=list)
