from datetime import date, datetime
from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BacktestRuleConfig(BaseModel):
    """回测规则配置，定义买卖条件、仓位和执行配置。"""
    buy_conditions: dict = Field(default_factory=dict)
    sell_conditions: dict = Field(default_factory=dict)
    position_config: dict = Field(default_factory=dict)
    execution_config: dict = Field(default_factory=dict)


# ---------- v2: 条件树结构 ----------

class ConditionLeaf(BaseModel):
    """条件叶子节点，表示单个字段的比较条件。"""
    field: str
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq", "in", "not_in"]
    value: Any
    params: Optional[dict] = None


class ConditionGroup(BaseModel):
    """条件组节点，支持 AND/OR 逻辑。"""
    logic: Literal["AND", "OR"]
    conditions: List[Union["ConditionGroup", ConditionLeaf]]

    @field_validator("conditions")
    @classmethod
    def non_empty(cls, v: list) -> list:
        if len(v) == 0:
            raise ValueError("Condition group must have at least one condition")
        return v


ConditionGroup.model_rebuild()


class BacktestRuleConfigV2(BaseModel):
    """回测规则配置 v2 版本，使用条件树结构定义买卖逻辑。"""
    version: Literal[2] = 2
    buy_conditions: ConditionGroup
    sell_conditions: ConditionGroup
    position_config: dict = Field(default_factory=dict)
    execution_config: dict = Field(default_factory=dict)


# ---------- 规则模板 CRUD ----------

class RuleTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    rule_config: dict


class RuleTemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = None
    rule_config: Optional[dict] = None


class RuleTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    rule_config: dict
    created_at: datetime
    updated_at: datetime


class BacktestCostConfig(BaseModel):
    """回测成本配置，包括佣金、印花税、滑点等交易成本。"""
    commission_rate: float = Field(default=0.0003, ge=0, le=1)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax_rate: float = Field(default=0.001, ge=0, le=1)
    slippage_rate: float = Field(default=0.001, ge=0, le=1)


class BacktestRunRequest(BaseModel):
    """回测运行请求参数。"""
    portfolio_id: int
    symbol_ids: list[int] = Field(min_length=1)
    start_date: date
    end_date: date
    rule_config: Union[BacktestRuleConfigV2, BacktestRuleConfig]
    cost_config: BacktestCostConfig | None = None
    run_name: str | None = None
    score_weight_mode: Literal['manual', 'ridge'] | None = None
    factor_model_run_id: str | None = None


class BacktestConditionTrace(BaseModel):
    field: str
    operator: str
    expected: Any = None
    actual: Any = None
    matched: bool
    indicator_key: str | None = None
    indicator_name: str | None = None
    value_type: str | None = None


class BacktestTradeRead(BaseModel):
    """回测交易记录，包含入场/出场信息和条件追踪。"""
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
    entry_traces: list[BacktestConditionTrace] = Field(default_factory=list)
    exit_traces: list[BacktestConditionTrace] = Field(default_factory=list)


class BacktestRunRead(BaseModel):
    """回测运行结果摘要，包含核心性能指标。"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    portfolio_id: int
    run_name: str
    symbols_json: str
    rule_config_json: str
    cost_config_json: str | None = None
    score_weight_mode: str = 'manual'
    factor_model_run_id: str | None = None
    factor_data_cutoff_at: datetime | None = None
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
    """回测运行详情，包含交易记录、价格曲线和诊断信息。"""
    trades: list[BacktestTradeRead] = Field(default_factory=list)
    price_series: list[dict] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)



