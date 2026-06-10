from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ScanRunCreate(BaseModel):
    preset_id: int | None = None
    portfolio_id: int | None = None
    portfolio_rule_id: int | None = None
    scope_snapshot: dict[str, Any]
    filters_snapshot: dict[str, Any] | None = None
    run_name: str | None = None


class ScanResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scan_run_id: int
    symbol_id: int
    result_type: str
    rank_no: int
    quality_score: float | None = None
    timing_score: float | None = None
    priority_score: float | None = None
    stage: str | None = None
    action: str | None = None
    recommended_position_pct: float | None = None
    is_sector_overweight: int
    is_asset_overweight: int
    reason_tags: str | None = None
    created_at: datetime

