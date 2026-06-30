from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class BacktestRuleConfig(BaseModel):
    """回测规则配置"""
    buy_conditions: dict = Field(default_factory=dict)
    sell_conditions: dict = Field(default_factory=dict)
    position_config: dict = Field(default_factory=dict)


class BacktestCostConfig(BaseModel):
    """交易成本配置"""
    commission_rate: float = Field(default=0.0003, ge=0, le=1)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax_rate: float = Field(default=0.001, ge=0, le=1)
    slippage_rate: float = Field(default=0.001, ge=0, le=1)


class BacktestRunRequest(BaseModel):
    """回测运行请求"""
    portfolio_id: int
    symbol_ids: list[int] = Field(min_length=1)
    start_date: date
    end_date: date
    rule_config: BacktestRuleConfig
    cost_config: BacktestCostConfig | None = None
    run_name: str | None = None


class BacktestTradeRead(BaseModel):
    """回测交易记录"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    run_id: int
    symbol_id: int
    entry_date: date
    entry_price: float
    quantity: float
    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    hold_days: int | None = None
    entry_cost: float
    exit_cost: float | None = None


class BacktestRunRead(BaseModel):
    """回测运行记录"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    portfolio_id: int
    run_name: str
    symbols_json: str
    rule_config_json: str
    cost_config_json: str | None = None
    start_date: date
    end_date: date
    initial_capital: float
    total_return: float | None = None
    total_return_pct: float | None = None
    max_drawdown: float | None = None
    max_drawdown_pct: float | None = None
    sharpe_ratio: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    trade_count: int | None = None
    avg_holding_days: float | None = None
    equity_curve_json: str | None = None
    status: str
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BacktestRunDetail(BacktestRunRead):
    """回测运行详情（含交易记录）"""
    trades: list[BacktestTradeRead] = []