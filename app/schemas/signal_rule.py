from pydantic import BaseModel, Field


class SignalRulePreset(BaseModel):
    mode: str
    rule_name: str
    description: str
    quality_tolerance: float
    timing_tolerance: float
    min_sample_count: int
    max_samples: int
    same_region: bool
    same_asset_type: bool
    same_stage: bool
    same_action: bool


class SignalRuleUpsert(BaseModel):
    rule_name: str = Field(default="balanced")
    mode: str = Field(default="balanced")
    quality_tolerance: float = Field(default=12.0, ge=1, le=40)
    timing_tolerance: float = Field(default=12.0, ge=1, le=40)
    min_sample_count: int = Field(default=3, ge=1, le=50)
    max_samples: int = Field(default=60, ge=5, le=240)
    same_region: bool = True
    same_asset_type: bool = True
    same_stage: bool = True
    same_action: bool = True


class SignalRuleRead(SignalRuleUpsert):
    id: int | None = None
    portfolio_id: int
    is_active: bool = True


class SignalRulePreviewRequest(SignalRuleUpsert):
    symbol_id: int | None = None


class SignalRulePreviewRead(BaseModel):
    symbol_id: int | None = None
    symbol: str | None = None
    name: str | None = None
    status: str
    message: str
    stats: dict | None = None
