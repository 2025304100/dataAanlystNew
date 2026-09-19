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


class MarketDataBatchRepairRequest(BaseModel):
    symbol_ids: list[int] = Field(..., min_length=1)
    start_date: date | None = None
    end_date: date | None = None
    adjust: str = "qfq"
    auto_score: bool = True


HistoryInitializationPreset = Literal["1m", "1q", "1y", "3y"]
HistoryInitializationStageStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
HistoryInitializationTaskStatus = Literal["idle", "running", "completed", "failed", "cancelled"]
HistoryInitializationRepairMode = Literal["both", "bars", "scores"]
HistoryInitializationSymbolSource = Literal[
    "all",          # 全部 is_active=1（默认）
    "watchlist",    # 观察池标的
    "positions",    # 持仓标的
    "scored",       # 有评分的标的
    "candidates",   # 最新扫描候选
    "cn-stock",     # A股
    "cn-etf",       # CN ETF
]


class HistoryInitializationRequest(BaseModel):
    preset: HistoryInitializationPreset = "1y"
    adjust: str = "qfq"
    asset_types: list[str] | None = None
    symbol_ids: list[int] | None = None
    symbol_source: HistoryInitializationSymbolSource = "all"
    repair_mode: HistoryInitializationRepairMode = "both"
    auto_scan: bool = False
    portfolio_id: int | None = None
    watchlist_id: int | None = None


class HistoryInitializationRetryRequest(BaseModel):
    task_id: str


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
    scan_run_id: int | None = None
    scan_executable_count: int = 0


class HistoryInitializationFailureItem(BaseModel):
    symbol_id: int
    symbol: str
    name: str | None = None
    asset_type: str | None = None
    stage: str
    message: str
    failed_days: int = 0
    last_trade_date: date | None = None


class HistoryInitializationRunRecord(BaseModel):
    task_id: str | None = None
    status: HistoryInitializationTaskStatus
    preset: HistoryInitializationPreset = "1y"
    adjust: str = "qfq"
    repair_mode: HistoryInitializationRepairMode = "both"
    asset_types: list[str] | None = None
    symbol_ids: list[int] = Field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: int | None = None
    message: str | None = None
    stages: list[HistoryInitializationStage] = Field(default_factory=list)
    failed_items: list[HistoryInitializationFailureItem] = Field(default_factory=list)
    summary: HistoryInitializationSummary = Field(default_factory=HistoryInitializationSummary)


class HistoryInitializationStatus(BaseModel):
    task_id: str | None = None
    status: HistoryInitializationTaskStatus
    preset: HistoryInitializationPreset = "1y"
    adjust: str = "qfq"
    repair_mode: HistoryInitializationRepairMode = "both"
    asset_types: list[str] | None = None
    symbol_ids: list[int] = Field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None
    progress_pct: int = 0
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: int | None = None
    stages: list[HistoryInitializationStage] = Field(default_factory=list)
    failed_items: list[HistoryInitializationFailureItem] = Field(default_factory=list)
    summary: HistoryInitializationSummary = Field(default_factory=HistoryInitializationSummary)
    recent_runs: list[HistoryInitializationRunRecord] = Field(default_factory=list)
