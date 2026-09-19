from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class SymbolCreate(BaseModel):
    symbol: str
    name: str
    asset_type: str
    market: str
    board: str | None = None
    industry: str | None = None
    theme: str | None = None
    listed_at: date | None = None


class SymbolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    name: str
    asset_type: str
    market: str
    region: str
    board: str | None = None
    industry: str | None = None
    theme: str | None = None
    listed_at: date | None = None
    created_at: datetime
