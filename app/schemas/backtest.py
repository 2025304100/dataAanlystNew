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
    # WP7.2 回测快照字段（向后兼容：历史 BacktestRun 这些字段为 None）
    member_snapshot_json: str | None = None
    symbol_ids_json: str | None = None
    excluded_members_json: str | None = None
    portfolio_rule_version_id: int | None = None
    score_mode: str | None = None
    data_cutoff_at: datetime | None = None
    engine_name: str | None = None
    engine_version: str | None = None
    source_type: str | None = None


class BacktestRunDetail(BacktestRunRead):
    """回测运行详情，包含交易记录、价格曲线和诊断信息。"""
    trades: list[BacktestTradeRead] = Field(default_factory=list)
    price_series: list[dict] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)


# ---------- P2-1：回测结果应用到模拟组合 ----------


class BacktestApplyRequest(BaseModel):
    """应用回测结果到组合的请求体。"""
    portfolio_id: int
    clear_existing: bool = False


class BacktestApplyResult(BaseModel):
    """应用回测结果到组合的响应。"""
    run_id: int
    portfolio_id: int
    initial_capital: float
    final_cash: float
    applied_trades: int
    skipped_trades: int
    open_positions: int
    errors: list[str] = Field(default_factory=list)


# ---------- P2-2：组合整体回测 ----------


class PortfolioBacktestRequest(BaseModel):
    """组合整体回测请求。

    前提：portfolio 必须是 simulated 账户且 auto_trade_enabled=1。
    symbol_ids 由后端自动推导（当前持仓 + 最新 scan executable 候选）。
    initial_capital 沿用 portfolio.total_capital（与单标的回测一致）。

    WP7.3 新增：
    - only_auto：仅回测 execution_mode='auto' 成员，跳过 manual/confirm 成员
      （仅在 PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=true 时生效）
    """
    portfolio_id: int
    start_date: date
    end_date: date
    run_name: str | None = None
    only_auto: bool = False
    # UI 主流程按“当前组合配置”做历史回测：使用当前持仓、当前有效成员和
    # 当前组合候选池作为静态标的范围，不按回测日期过滤今天才加入的标的。
    current_universe: bool = False
    # 基准指数名称（中文名映射到代码，如 "沪深300"→000300）
    benchmark: str | None = None

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        start = info.data.get("start_date")
        if start and v < start:
            raise ValueError("end_date must be >= start_date")
        return v


class PortfolioBacktestResult(BaseModel):
    """组合整体回测响应。"""
    run_id: int
    portfolio_id: int
    symbol_ids: list[int]
    symbol_count: int
    start_date: date
    end_date: date
    initial_capital: float
    status: str
    run_name: str
    # WP7.1/WP7.3：标的来源标签与排除成员数（旧来源回测时为 0）
    symbol_source: str | None = None
    source_type: str | None = None
    excluded_member_count: int = 0
    # P2-FIX(TDD RED→GREEN): 同步返回运行指标/曲线/明细，避免“回测已启动但没效果”
    # 对齐 BacktestRunRead/BacktestRunDetail 常用渲染字段，便于前端 POST 后直接用
    total_return: float | None = None
    total_return_pct: float | None = None
    max_drawdown: float | None = None
    max_drawdown_pct: float | None = None
    sharpe_ratio: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    trade_count: int | None = None
    avg_holding_days: float | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # equity_curve: 直接给前端可渲染的 {date,equity,cash,position_value,...} 列表（反序列化后的）
    # 保持与 runBacktest + BacktestRun.equity_curve_json 反序列化后一致的语义
    equity_curve: list[dict] = Field(default_factory=list)
    # trades: 回测交易明细（精简版，避免字段依赖 ORM rel 循环序列化）
    trades: list[BacktestTradeRead] = Field(default_factory=list)
    # metrics: 复用 BacktestRun 指标 + 前端 BacktestSummary 常用 key（冗余一份，向后兼容）
    metrics: dict = Field(default_factory=dict)
    # diagnostics: buy_signal_days / skip_reasons / sample_misses 等诊断（对齐 BacktestRunDetail.diagnostics）
    diagnostics: dict = Field(default_factory=dict)
    # 执行/校验警告与可恢复错误（便于前端 toast 之后直接展示给用户，不用再调 detail）
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# ---------- WP7.4：组合回测来源状态与新旧对比 ----------


class PortfolioBacktestSourceStatus(BaseModel):
    """组合回测标的来源开关状态。

    - enabled：当前是否启用历史成员来源（PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED）
    - env_flag：环境变量名
    - source_label：当前生效的来源标签（"legacy" / "members"）
    """
    enabled: bool
    env_flag: str
    source_label: str


class PortfolioBacktestCompareRequest(BaseModel):
    """新旧引擎对比请求。"""
    portfolio_id: int
    start_date: date
    end_date: date
    initial_capital: float | None = None
    run_name_prefix: str = "compare"

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        start = info.data.get("start_date")
        if start and v < start:
            raise ValueError("end_date must be >= start_date")
        return v


class PortfolioBacktestCompareResult(BaseModel):
    """新旧引擎对比响应。

    old/new 各自包含 run_id、symbol_ids、source_type、trades、metrics；
    diff 包含标的集差异、关键指标差异与文本解释。
    """
    model_config = ConfigDict(from_attributes=True)

    old: dict
    new: dict
    diff: dict


