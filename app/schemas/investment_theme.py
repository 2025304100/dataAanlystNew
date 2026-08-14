from datetime import datetime

from pydantic import BaseModel, Field


class InvestmentThemeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    source_type: str = "manual"


class ThemeSymbolMappingCreate(BaseModel):
    symbol_id: int
    source_type: str = "manual"
    source_ref: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    is_confirmed: bool = True


class ThemeCatalystCreate(BaseModel):
    market_event_id: int | None = None
    title: str = Field(min_length=1)
    catalyst_score: float = Field(ge=0, le=100)
    confidence: float = Field(default=1.0, ge=0, le=1)
    source_type: str = "manual"
    source_ref: str | None = None
    published_at: datetime | None = None
    expires_at: datetime | None = None
