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
    # 回测与统一决策证据的可选关联。历史回测可能没有这些字段。
    decision_evidence_id: str | None = None
    entry_decision_run_id: str | None = None
    exit_evidence_id: str | None = None
    exit_decision_run_id: str | None = None
    intended_entry_price: float | None = None
    intended_exit_price: float | None = None
    slippage_bps: float | None = None
    entry_rejection_reason: str | None = None
    entry_requested_quantity: float | None = None
    entry_filled_quantity: float | None = None
    entry_remaining_quantity: float | None = None
    entry_order_plan_status: str | None = None
    entry_unfilled_reason: str | None = None
    exit_requested_quantity: float | None = None
    exit_filled_quantity: float | None = None
    exit_remaining_quantity: float | None = None
    exit_order_plan_status: str | None = None
    exit_unfilled_reason: str | None = None


class BacktestPositionRead(BaseModel):
    """One immutable day/symbol row in a backtest holding ledger.

    The result center deliberately uses a different shape from the transaction
    ledger: every row describes the opening position, the day's buy/sell
    changes, the closing position and its end-of-day weight.  Reading it never
    writes a new portfolio state.
    """

    run_id: int
    symbol_id: int
    trade_date: date
    opening_quantity: float
    buy_quantity: float
    sell_quantity: float
    closing_quantity: float
    status: Literal["OPEN", "CLOSED"]
    as_of_date: date
    mark_price: float | None = None
    market_value: float | None = None
    portfolio_equity: float | None = None
    weight: float | None = None
    buy_evidence_ids: list[str] = Field(default_factory=list)
    sell_evidence_ids: list[str] = Field(default_factory=list)


class BacktestPositionPage(BaseModel):
    """Paginated immutable daily holding-ledger response."""

    total: int
    page: int
    page_size: int
    as_of_date: date
    status: Literal["OPEN", "CLOSED"] | None = None
    ledger_mode: Literal["EXECUTION_EVENTS", "LEGACY_TRADE_APPROXIMATION"]
    items: list[BacktestPositionRead] = Field(default_factory=list)


class BacktestRunRead(BaseModel):
    """回测运行结果摘要，包含核心性能指标。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    run_name: str
    symbols_json: str
    rule_config_json: str
    cost_config_json: str | None = None
    # 规范化后的成本配置（新运行可直接回显；历史运行仅保留原始 JSON）。
    cost_config: dict[str, Any] | None = None
    score_weight_mode: str = 'manual'
    factor_model_run_id: str | None = None
    factor_set_id: str | None = None
    factor_data_cutoff_at: datetime | None = None
    strategy_snapshot_id: str | None = None
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
    reproducibility_status: str = "legacy/non_reproducible"
    reproducibility_reason: str | None = None
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
    # 执行/PIT 参数快照。历史运行没有这些列时保持 None。
    pit_mode: str | None = None
    match_mode: str | None = None
    benchmark: str | None = None
    benchmark_code: str | None = None
    commission_rate: float | None = None
    stamp_tax_rate: float | None = None
    slippage_bps: float | None = None
    price_type: str | None = None
    volume_limit_pct: float | None = None
    rebalance_frequency: str | None = None
    # 基准健康状态与持久化曲线（缺失基准不得伪造收益线）。
    benchmark_equity_json: str | None = None
    benchmark_status: str | None = None
    benchmark_gap_days: int | None = None
    # 快照/门禁只读元数据。具体内容仍可从 decision_snapshot 读取。
    snapshot_no: int | None = None
    snapshot_hash: str | None = None
    gate_policy_version: str | None = None
    gate_result: str | None = None
    gate_result_json: Any = None
    blocking_status: str | None = None
    blocking_reasons: Any = None
    is_result_production_eligible: bool = True


class BacktestRunDetail(BacktestRunRead):
    """回测运行详情，不内嵌无限制交易流水。

    交易流水只能通过 ``/backtest/runs/{run_id}/trades`` 的分页契约读取，
    避免历史运行详情把数年记录一次性载入浏览器。
    """
    price_series: list[dict] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)
    # Q29：回测只引用已经持久化的 DecisionRun，绝不在详情查询时补造证据。
    decision_run_ids: list[str] = Field(default_factory=list)
    evidence_summary: dict = Field(default_factory=dict)
    rejected_count: int = 0
    decision_snapshot: dict | None = None
    # 便于结果区直接渲染，不要求客户端再次解析运行字段。
    benchmark_equity: list[dict] = Field(default_factory=list)
    data_snapshot: dict[str, Any] | None = None


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
    symbol_ids 由后端自动推导（候选池 + 持仓的并集，统一三段口径）。

    WP0-5 / C-05 契约参数完整化（8 项入参，与 tasks.md TR-05.3 / spec §22.3.1 对齐）：
      - initial_capital ：覆盖 portfolio 默认 total_capital
      - commission_rate / stamp_tax_rate / slippage_bps ：交易成本三要素
      - benchmark : 基准名称（中文映射到真实指数代码）
      - price_type : NEXT_OPEN / T_CLOSE（T_CLOSE 仅研究模式）
      - volume_limit_pct : 成交量占比限制（防冲击）
      - rebalance_frequency : daily / weekly / monthly / on_signal

    WP0-5 / C-03 契约收紧：不再暴露 only_auto / current_universe，
    证券范围统一使用 as-of 日期的 PortfolioCandidate + 当前持仓，避免"前端特殊分支
    绕过 PIT 成员资格"的假回测（与回测/自动模拟/预检三入口一致）。

    WP0-5a 新请求契约 (Q8.2)：
    - strategy_snapshot_id：任务启动时锁定的不可变执行快照；
      若为空，后端自动按当前绑定创建任务级快照锁
    - score_weight_mode / factor_model_run_id：显式覆盖（仅研究模式）；
      正式 PIT 链路必须依赖 strategy_snapshot_id 中不可变绑定，禁止自由覆盖。
    - pit_mode：research_pit / production_pit / legacy_research（production_pit 严格 PIT_VERIFIED 门禁）
    """
    model_config = ConfigDict(extra="forbid")  # C-08：严禁前端注入 universe_type 等伪池字段

    portfolio_id: int
    start_date: date
    end_date: date
    run_name: str | None = None
    benchmark: str | None = None
    strategy_snapshot_id: str | None = None
    score_weight_mode: str | None = None
    factor_model_run_id: str | None = None
    # ↓ WP0-5 新增 8 契约参数（TR-05.3）
    initial_capital: float | None = Field(default=None, gt=0)
    commission_rate: float = Field(default=0.0003, ge=0, le=1)
    stamp_tax_rate: float = Field(default=0.001, ge=0, le=1)
    slippage_bps: int = Field(default=5, ge=0, le=10_000)
    price_type: Literal["NEXT_OPEN", "T_CLOSE"] = "NEXT_OPEN"
    volume_limit_pct: float = Field(default=0.10, ge=0, le=1)
    rebalance_frequency: Literal["daily", "weekly", "monthly", "on_signal"] = "on_signal"
    pit_mode: Literal["legacy_research", "research_pit", "production_pit"] = "legacy_research"

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        start = info.data.get("start_date")
        if start and v < start:
            raise ValueError("end_date must be >= start_date")
        return v

    @field_validator("score_weight_mode")
    @classmethod
    def _valid_mode(cls, v):
        if v is None or v == "":
            return None
        if v not in {"manual", "ridge"}:
            raise ValueError("score_weight_mode 仅允许 manual/ridge")
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
    # 请求参数回显（服务层已生成这些字段；旧服务响应可省略）。
    benchmark: str | None = None
    benchmark_code: str | None = None
    commission_rate: float | None = None
    stamp_tax_rate: float | None = None
    slippage_bps: float | None = None
    price_type: str | None = None
    volume_limit_pct: float | None = None
    rebalance_frequency: str | None = None
    pit_mode: str | None = None
    cost_config: dict[str, Any] | None = None
    # 运行锁定的不可变策略/数据快照。
    strategy_snapshot_id: str | None = None
    snapshot_no: int | None = None
    snapshot_hash: str | None = None
    factor_model_run_id: str | None = None
    factor_set_id: str | None = None
    factor_data_cutoff_at: datetime | None = None
    data_cutoff_at: datetime | None = None
    data_snapshot: dict[str, Any] | None = None
    member_snapshot_json: str | None = None
    excluded_members_json: str | None = None
    # 基准曲线与数据健康状态。
    benchmark_equity: list[dict] = Field(default_factory=list)
    benchmark_equity_json: str | None = None
    benchmark_status: str | None = None
    benchmark_gap_days: int | None = None
    # 门禁结果必须可回显，阻断运行不能被 UI 误认为“无信号”。
    gate_policy_version: str | None = None
    gate_result: str | None = None
    gate_result_json: Any = None
    blocking_status: str | None = None
    blocking_reasons: Any = None
    is_result_production_eligible: bool = True
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
    # 交易流水不在运行摘要中返回。客户端仅通过
    # GET /backtest/runs/{run_id}/trades?page=&page_size= 获取分页数据。
    # metrics: 复用 BacktestRun 指标 + 前端 BacktestSummary 常用 key（冗余一份，向后兼容）
    metrics: dict = Field(default_factory=dict)
    # diagnostics: buy_signal_days / skip_reasons / sample_misses 等诊断（对齐 BacktestRunDetail.diagnostics）
    diagnostics: dict = Field(default_factory=dict)
    # 执行/校验警告与可恢复错误（便于前端 toast 之后直接展示给用户，不用再调 detail）
    warnings: list[str | dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # Q29：统一决策链路关联（历史回测可能为空）。
    decision_run_ids: list[str] = Field(default_factory=list)
    evidence_summary: dict = Field(default_factory=dict)
    rejected_count: int = 0
    decision_snapshot: dict | None = None


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
