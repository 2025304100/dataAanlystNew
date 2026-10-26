from datetime import datetime

from pydantic import BaseModel, Field


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


class MarketEventCollectResponse(BaseModel):
    collected: int
    skipped_duplicate: int
    errors: list[str] = Field(default_factory=list)


class MarketEventCleanupRequest(BaseModel):
    """P0-1：过期消息清理参数。keep_days 为发布时间保留窗口，默认 90 天。"""
    keep_days: int = Field(default=90, ge=7, le=3650)


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
