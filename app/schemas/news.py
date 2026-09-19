from datetime import datetime

from pydantic import BaseModel, Field


class NewsUpdateRequest(BaseModel):
    portfolio_id: int | None = None
    scope: str = Field(default="candidates")
    symbol_ids: list[int] | None = None
    watchlist_id: int | None = None
    days: int = Field(default=7, ge=1, le=30)
    include_macro: bool = True
    include_sector: bool = True
    include_symbol: bool = True


class NewsEventRead(BaseModel):
    id: int | None = None
    symbol_id: int | None = None
    symbol: str | None = None
    title: str
    source: str
    url: str | None = None
    event_type: str
    sentiment: str
    strength: int
    effective_score: float
    risk_level: str
    published_at: datetime | None = None
    expires_at: datetime | None = None


class NewsSymbolSummary(BaseModel):
    symbol_id: int
    symbol: str
    name: str
    message_score: float
    sentiment: str
    risk_level: str
    confidence: float
    positive_count: int
    negative_count: int
    risk_count: int
    latest_title: str | None = None
    events: list[NewsEventRead] = Field(default_factory=list)


class NewsMacroSummary(BaseModel):
    message_score: float
    sentiment: str
    risk_level: str
    summary: str
    events: list[NewsEventRead] = Field(default_factory=list)


class NewsUpdateResponse(BaseModel):
    scope: str
    days: int
    symbols_total: int
    macro: NewsMacroSummary | None = None
    symbols: list[NewsSymbolSummary]
    failed: list[dict] = Field(default_factory=list)
