from datetime import datetime, date

from pydantic import BaseModel


class DashboardOverview(BaseModel):
    symbols_count: int
    watchlists_count: int
    total_position_pct: float
    cash_pct: float
    top_candidates: list[dict]
    risk_flags: list[str]


class WorkbenchCandidate(BaseModel):
    symbol_id: int
    symbol: str
    name: str
    market: str
    region: str
    asset_type: str
    quality_score: float | None = None
    timing_score: float | None = None
    priority_score: float | None = None
    trend_score: float | None = None
    momentum_score: float | None = None
    volatility_score: float | None = None
    liquidity_score: float | None = None
    breadth_score: float | None = None
    event_score: float | None = None
    stage: str | None = None
    action: str | None = None
    recommended_position_pct: float | None = None
    rank_no: int | None = None


class WorkbenchScore(BaseModel):
    symbol_id: int
    symbol: str
    name: str
    market: str
    region: str
    asset_type: str
    trade_date: date
    quality_score: float
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


class WorkbenchWatchlist(BaseModel):
    id: int
    name: str
    list_type: str
    item_count: int


class WorkbenchJournal(BaseModel):
    id: int
    title: str
    entry_type: str
    symbol_id: int
    created_at: datetime


class WorkbenchLatestScan(BaseModel):
    scan_run_id: int | None = None
    run_name: str | None = None
    created_at: datetime | None = None
    executable_count: int = 0
    total_results: int = 0
    auto_scan: bool = False


class WorkbenchBar(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class WorkbenchActiveRule(BaseModel):
    id: int
    rule_name: str
    max_single_position_pct: float
    max_stock_position_pct: float
    max_etf_position_pct: float
    max_open_positions: int


class WorkbenchMarketScope(BaseModel):
    selected_group: str = "all"
    available_groups: list[str]
    total_symbols: int
    filtered_symbols: int
    region_counts: dict[str, int]


class WorkbenchAccountSummary(BaseModel):
    cash_balance: float
    available_cash: float
    market_value: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    cash_pct: float
    invested_pct: float
    position_count: int
    trade_count_7d: int
    last_trade_at: datetime | None = None


class WorkbenchTrade(BaseModel):
    id: int
    symbol_id: int
    symbol: str
    name: str
    side: str
    quantity: float
    price: float
    amount: float
    fee: float
    realized_pnl: float | None = None
    created_at: datetime


class WorkbenchSymbolDetail(BaseModel):
    symbol: dict
    latest_score: dict | None = None
    latest_trade_setup: dict | None = None
    signal_stats: dict | None = None
    position: dict | None = None
    score_history: list[dict]
    bars: list[WorkbenchBar]
    journals: list[WorkbenchJournal]
    recent_trades: list[WorkbenchTrade] = []


class DashboardWorkbench(BaseModel):
    portfolio: dict | None = None
    active_rule: WorkbenchActiveRule | None = None
    market_scope: WorkbenchMarketScope
    overview: DashboardOverview
    account_summary: WorkbenchAccountSummary | None = None
    latest_scan: WorkbenchLatestScan
    candidates: list[WorkbenchCandidate]
    latest_scores: list[WorkbenchScore]
    recent_trades: list[WorkbenchTrade] = []
    watchlists: list[WorkbenchWatchlist]
    journals: list[WorkbenchJournal]
