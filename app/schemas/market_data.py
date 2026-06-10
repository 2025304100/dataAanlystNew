from datetime import date

from pydantic import BaseModel, ConfigDict


class DailyBarItem(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    source: str = "manual"


class DailyBarImportRequest(BaseModel):
    symbol_id: int
    bars: list[DailyBarItem]


class DailyBarRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol_id: int
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    source: str


class MarketDataUpdateRequest(BaseModel):
    scope: str = "all"
    watchlist_id: int | None = None
    symbol_ids: list[int] | None = None
    asset_types: list[str] | None = None
    start_date: date | None = None
    end_date: date | None = None
    adjust: str = "qfq"
    auto_scan: bool = True
    portfolio_id: int | None = None
    portfolio_rule_id: int | None = None
