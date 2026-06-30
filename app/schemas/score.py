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
    # 股质评分分项
    trend_score: float | None = None
    momentum_score: float | None = None
    volatility_score: float | None = None
    liquidity_score: float | None = None
    breadth_score: float | None = None
    event_score: float | None = None
    # 时点评分分项（新增）
    breakout_score: float | None = None
    pullback_score: float | None = None
    overheat_penalty: float | None = None
    # 数据可信度（P0-4.3）
    data_credibility: float | None = None
    created_at: datetime

