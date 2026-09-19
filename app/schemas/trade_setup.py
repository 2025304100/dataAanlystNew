from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TradeSetupOverrides(BaseModel):
    entry_min: float | None = Field(default=None, ge=0)
    entry_max: float | None = Field(default=None, ge=0)
    stop_loss: float | None = Field(default=None, ge=0)
    target_price: float | None = Field(default=None, ge=0)
    recommended_position_pct: float | None = Field(default=None, ge=0, le=1)
    recommended_position_amount: float | None = Field(default=None, ge=0)


class TradeSetupTranche(BaseModel):
    label: str = Field(default="Custom", max_length=64)
    position_pct: float = Field(default=0, ge=0, le=1)
    amount: float = Field(default=0, ge=0)
    trigger: str = Field(default="", max_length=300)


class TradeSetupTrancheUpdateRequest(BaseModel):
    tranche_plan: list[TradeSetupTranche] = Field(default_factory=list, max_length=10)


class TradeSetupGenerateRequest(BaseModel):
    portfolio_id: int
    symbol_id: int
    score_id: int | None = None
    scan_run_id: int | None = None
    overrides: TradeSetupOverrides | None = None


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
    manual_overrides_json: str | None = None
    field_sources_json: str | None = None
    manual_tranche_plan_json: str | None = None
    manual_overrides: dict[str, float | None] | None = None
    field_sources: dict[str, str] | None = None
    created_at: datetime
