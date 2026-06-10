from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WatchlistCreate(BaseModel):
    name: str
    list_type: str
    description: str | None = None


class WatchlistRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    list_type: str
    description: str | None = None
    created_at: datetime


class WatchlistItemCreate(BaseModel):
    symbol_id: int
    note: str | None = None


class WatchlistItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    watchlist_id: int
    symbol_id: int
    note: str | None = None
    added_at: datetime

