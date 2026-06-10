from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class ScoreCalculationRequest(BaseModel):
    symbol_ids: list[int]
    trade_date: date


class ScoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol_id: int
    trade_date: date
    quality_score: float
    quality_grade: str
    timing_score: float
    stage: str
    action: str
    priority_score: float
    trend_score: float | None = None
    momentum_score: float | None = None
    volatility_score: float | None = None
    liquidity_score: float | None = None
    breadth_score: float | None = None
    event_score: float | None = None
    created_at: datetime

