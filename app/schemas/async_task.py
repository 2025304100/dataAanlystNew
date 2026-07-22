from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator


class AsyncTaskRead(BaseModel):
    """异步任务状态响应。"""

    id: str
    task_type: str
    status: str
    stage: str
    percent: float
    message: str
    total: int
    processed: int
    ok_count: int
    failed_count: int
    current_item: str | None = None
    result: dict | None = None
    errors: list[dict] = Field(default_factory=list)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None
    # WP-S.5 任务防卡死状态机扩展字段（全部可选，向后兼容旧前端）
    heartbeat_at: datetime | None = None
    stage_budget_seconds: int | None = None
    stage_started_at: datetime | None = None
    last_progress_at: datetime | None = None
    last_progress_percent: float | None = None
    current_step_description: str | None = None
    suggested_action: str | None = None
    batch_recovery: list[dict] | None = None
    last_patrol_at: datetime | None = None
    cancel_requested: bool = False


class MarketDataSyncCreate(BaseModel):
    """创建市场数据异步同步任务的请求体。"""

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


class FactorPipelineCreate(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    data_cutoff_date: date | None = None
    full_refresh: bool = False
    train_model: bool = True
    materialize_scores: bool = True
    window_days: int = Field(default=250, ge=60, le=1000)
    validation_days: int = Field(default=50, ge=20, le=250)

    @model_validator(mode='after')
    def validate_training_windows(self):
        if self.validation_days >= self.window_days:
            raise ValueError('validation_days must be less than window_days')
        return self
