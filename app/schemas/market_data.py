from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class MarketDataRepairRequest(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    adjust: str = "qfq"
    auto_score: bool = True


HistoryInitializationPreset = Literal["1m", "1q", "1y", "3y"]
HistoryInitializationStageStatus = Literal["pending", "running", "completed", "failed"]
HistoryInitializationTaskStatus = Literal["idle", "running", "completed", "failed"]


class HistoryInitializationRequest(BaseModel):
    preset: HistoryInitializationPreset = "1y"
    adjust: str = "qfq"
    asset_types: list[str] | None = None


class HistoryInitializationStage(BaseModel):
    key: str
    status: HistoryInitializationStageStatus
    percent: int
    done: int
    total: int
    message: str | None = None


class HistoryInitializationSummary(BaseModel):
    symbols_total: int = 0
    sync_ok_count: int = 0
    sync_failed_count: int = 0
    empty_count: int = 0
    bars_rows: int = 0
    score_days_total: int = 0
    score_days_completed: int = 0


class HistoryInitializationRunRecord(BaseModel):
    task_id: str | None = None
    status: HistoryInitializationTaskStatus
    preset: HistoryInitializationPreset = "1y"
    adjust: str = "qfq"
    start_date: date | None = None
    end_date: date | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: int | None = None
    message: str | None = None
    summary: HistoryInitializationSummary = Field(default_factory=HistoryInitializationSummary)


class HistoryInitializationStatus(BaseModel):
    task_id: str | None = None
    status: HistoryInitializationTaskStatus
    preset: HistoryInitializationPreset = "1y"
    adjust: str = "qfq"
    start_date: date | None = None
    end_date: date | None = None
    progress_pct: int = 0
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: int | None = None
    stages: list[HistoryInitializationStage] = Field(default_factory=list)
    summary: HistoryInitializationSummary = Field(default_factory=HistoryInitializationSummary)
    recent_runs: list[HistoryInitializationRunRecord] = Field(default_factory=list)
