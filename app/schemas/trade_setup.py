from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TradeSetupGenerateRequest(BaseModel):
    portfolio_id: int
    symbol_id: int
    score_id: int | None = None
    scan_run_id: int | None = None


class TradeSetupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    symbol_id: int
    score_id: int
    scan_run_id: int | None = None
    stage: str
    action: str
    entry_min: float | None = None
    entry_max: float | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    recommended_position_pct: float
    recommended_position_amount: float
    risk_reward_ratio: float | None = None
    allow_add_position: int
    is_sector_overweight: int
    is_asset_overweight: int
    setup_reason: str | None = None
    created_at: datetime
