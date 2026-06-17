from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DiscoveryTaskCreate(BaseModel):
    scope: Literal["cn-stock", "cn-etf", "us-stock", "us-etf"] = Field(default="cn-stock")
    min_score: float = Field(default=55, ge=0, le=100)
    include_news: bool = True
    portfolio_id: int | None = None
    portfolio_rule_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    adjust: str = "qfq"
    symbol_limit: int | None = Field(default=None, ge=1, le=10000)
    batch_size: int = Field(default=20, ge=1, le=100)
    delay_seconds: float = Field(default=0.25, ge=0, le=5)
    news_limit: int = Field(default=30, ge=0, le=100)
    global_mode: str = "library"
    refresh_universe: bool = True
    warning_days: int = Field(default=3, ge=1, le=60)
    valid_days: int = Field(default=5, ge=1, le=365)


class DiscoveryTaskRead(BaseModel):
    id: str
    status: str
    stage: str
    percent: float
    message: str
    scope: str
    min_score: float
    include_news: bool
    total: int
    processed: int
    ok_count: int
    failed_count: int
    empty_count: int
    scored_count: int
    current_symbol: str | None = None
    batch_size: int
    delay_seconds: float
    adaptive_delay_seconds: float
    symbol_limit: int | None = None
    scan_run_id: int | None = None
    executable_count: int = 0
    news_symbols_total: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    paused_at: datetime | None = None
    cancelled_at: datetime | None = None
    finished_at: datetime | None = None
    can_resume: bool = False


class DiscoveryResultUpdate(BaseModel):
    is_frozen: bool | None = None
    warning_days: int | None = Field(default=None, ge=1, le=60)
    valid_days: int | None = Field(default=None, ge=1, le=365)
