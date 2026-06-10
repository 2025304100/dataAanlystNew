from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PortfolioCreate(BaseModel):
    name: str
    account_type: str
    total_capital: float
    investable_ratio: float
    cash_reserve_ratio: float
    currency: str = "CNY"
    is_default: bool = False


class PortfolioRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    account_type: str
    total_capital: float
    investable_ratio: float
    cash_reserve_ratio: float
    currency: str
    is_default: int
    created_at: datetime


class PortfolioRuleUpsert(BaseModel):
    rule_name: str
    max_single_position_pct: float
    max_sector_position_pct: float
    max_stock_position_pct: float
    max_etf_position_pct: float
    max_loss_per_trade_pct: float
    max_open_positions: int
    stage_limits_json: dict[str, Any]
    is_active: bool = True


class AllocationSummary(BaseModel):
    total_position_pct: float
    stock_position_pct: float
    etf_position_pct: float
    cash_pct: float
    position_count: int
    sector_exposure: dict[str, float]
    remaining_stock_pct: float
    remaining_etf_pct: float


class PositionUpsert(BaseModel):
    symbol_id: int
    quantity: float
    avg_cost: float
    latest_price: float


class PositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    symbol_id: int
    quantity: float
    avg_cost: float
    latest_price: float
    market_value: float
    position_pct: float
    asset_type: str
    theme: str | None = None
