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
    # WPD-05: 顶层 error_code，从 errors_json[0].error_code 提取，作为前端 i18n 映射的稳定事实来源
    error_code: str | None = None
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
    # WP5: 幂等检查和前端 fingerprint 展示需要原始 payload JSON（可选，不破坏旧契约）
    payload_json: str | None = None
    # WP5: 便捷顶层 fingerprint（可由前端直接展示或用于对比）
    fingerprint: str | None = None


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
    # WP7-03: 指定 FactorSet 进行 Ridge 训练（不传则回退到静态 FEATURE_CODES）
    factor_set_id: str | None = None

    @model_validator(mode='after')
    def validate_training_windows(self):
        if self.validation_days >= self.window_days:
            raise ValueError('validation_days must be less than window_days')
        return self
