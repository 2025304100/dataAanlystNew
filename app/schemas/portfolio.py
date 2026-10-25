import json as _json
import sys
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.core.storage_limits import validate_mysql_text


# Preserve zero-capital portfolios and each ratio's independent meaning. The
# capital ceiling only prevents overflow when money is converted to cents.
CapitalAmount = Annotated[float, Field(ge=0, le=sys.float_info.max / 100, allow_inf_nan=False)]
Ratio = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
PortfolioName = Annotated[str, Field(min_length=1, max_length=128)]
BenchmarkCode = Annotated[str, Field(min_length=1, max_length=32)]
KeyMemberId = Annotated[int, Field(gt=0, strict=True)]


class _PortfolioWrite(BaseModel):
    @field_validator("key_members_json", check_fields=False)
    @classmethod
    def _unique_key_members(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("关键成员标的 ID 不能重复")
        if value is not None:
            validate_mysql_text(_json.dumps(value), field="key_members_json")
        return value

    @field_validator("name", "benchmark_code", mode="before", check_fields=False)
    @classmethod
    def _trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("total_capital", "investable_ratio", "cash_reserve_ratio", "buy_fee_pct",
                     "sell_fee_pct", "default_single_position_pct", mode="before", check_fields=False)
    @classmethod
    def _reject_boolean_number(cls, value):
        if isinstance(value, bool):
            raise ValueError("金额和比例不能使用布尔值")
        return value


def _parse_json_text_field(v: Any) -> Any:
    if isinstance(v, str):
        if not v:
            return None
        try:
            return _json.loads(v)
        except Exception:
            return v
    return v


class PortfolioCreate(_PortfolioWrite):
    name: PortfolioName
    # 合法值与前端 CreatePortfolioModal 一致：simulated（模拟）/ manual（真实手动账户）。
    # 模拟交易、自动交易、权益快照等链路都只认 simulated，脏值会导致后续行为不可预期。
    account_type: Literal["simulated", "manual"]
    asset_scope: Literal["stock", "etf", "mixed"] = "mixed"
    total_capital: CapitalAmount
    investable_ratio: Ratio
    cash_reserve_ratio: Ratio
    currency: str = Field("CNY", pattern=r"^[A-Z]{3}$")
    is_default: bool = False
    auto_trade_enabled: bool = False
    # P1-FIX: 创建组合补充字段（前端曾只存本地状态未提交）
    # 佣金：ratio（0.00025 = 0.025%）
    buy_fee_pct: Ratio = 0.00025
    sell_fee_pct: Ratio = 0.00025
    # 基准指数代码，如 000300 / 000905 / 399006 等
    benchmark_code: BenchmarkCode = "000300"
    # 默认单票仓位上限 ratio，例如 0.3 = 30%
    default_single_position_pct: Ratio = 0.30
    # Q5.3: 关键成员 symbol_id 列表；None/空=全组合默认关键
    key_members_json: list[KeyMemberId] | None = None


class PortfolioUpdate(_PortfolioWrite):
    """组合部分更新（PUT）。

    所有字段可选，仅更新传入的字段。
    account_type 不允许修改（避免模拟账户与普通账户切换导致数据不一致）。
    """
    name: PortfolioName | None = None
    asset_scope: Literal["stock", "etf", "mixed"] | None = None
    total_capital: CapitalAmount | None = None
    investable_ratio: Ratio | None = None
    cash_reserve_ratio: Ratio | None = None
    currency: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    is_default: bool | None = None
    auto_trade_enabled: bool | None = None
    # P1-FIX: 允许增量更新补充字段
    buy_fee_pct: Ratio | None = None
    sell_fee_pct: Ratio | None = None
    benchmark_code: BenchmarkCode | None = None
    default_single_position_pct: Ratio | None = None
    # Q5.3: 关键成员 symbol_id 列表；None/空=全组合默认关键
    key_members_json: list[KeyMemberId] | None = None


class PortfolioRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    account_type: str
    asset_scope: str = "mixed"
    total_capital: float
    investable_ratio: float
    cash_reserve_ratio: float
    currency: str
    is_default: int
    auto_trade_enabled: int = 0
    auto_trade_last_run_at: datetime | None = None
    # P1-FIX: 读出补充字段
    buy_fee_pct: float = 0.00025
    sell_fee_pct: float = 0.00025
    benchmark_code: str = "000300"
    default_single_position_pct: float = 0.30
    # P2-FIX: 隔离测试组合（0=生产，1=测试）
    is_test: int = 0
    # Q5.3: 关键成员 symbol_id 列表；None/空=全组合默认关键
    key_members_json: list[int] | None = None
    created_at: datetime
    updated_at: datetime | None = None

    @field_validator("key_members_json", mode="before")
    @classmethod
    def _parse_key_members(cls, v):
        return _parse_json_text_field(v)


class AutoTradeExecuteRequest(BaseModel):
    """P2-3：手动触发自动交易的请求体。"""
    dry_run: bool = True
    buy_candidate_limit: int = 10


class AutoTradePlanItem(BaseModel):
    """单个买卖计划项。"""
    symbol_id: int
    symbol: str
    name: str
    action: str
    stage: str | None = None
    ref_price: float
    executed: bool = False
    order_id: int | None = None
    filled_price: float | None = None
    fee: float | None = None
    # 卖出特有
    held_quantity: float | None = None
    sell_quantity: float | None = None
    reason: str | None = None
    # 买入特有
    can_open: bool | None = None
    decision: str | None = None
    blocked_reasons: list[str] = []
    recommended_amount: float | None = None
    buy_quantity: float | None = None
    order_plan_id: str | None = None
    evidence_id: str | None = None
    quantity: float | None = None
    signal_date: date | None = None
    execution_date: date | None = None
    reason_code: str | None = None
    status: str | None = None
    filled_quantity: float | None = None


class AutoTradeResult(BaseModel):
    """P2-3：自动交易执行结果。"""
    portfolio_id: int
    dry_run: bool
    sells: list[AutoTradePlanItem]
    buys: list[AutoTradePlanItem]
    errors: list[str] = []
    executed_at: str
    # P0-AutoTrade：统一执行/来源诊断字段（增量兼容新增，前端可选消费）
    executed_source: str | None = None  # new / old
    member_source_enabled: bool | None = None
    source_mode: str | None = None  # portfolio / members_only / legacy_scan
    strategy_snapshot_id: str | None = None
    readiness: dict[str, Any] | None = None
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    # Recovery can fill an earlier immutable plan while today's decision is
    # HOLD. Preserve its execution receipt even when today's buys/sells is empty.
    execution_result: dict[str, Any] | None = None


class PortfolioRuleUpsert(BaseModel):
    rule_name: str
    max_single_position_pct: float
    max_sector_position_pct: float
    max_stock_position_pct: float
    max_etf_position_pct: float
    max_loss_per_trade_pct: float
    max_open_positions: int
    stage_limits_json: dict[str, Any]
    is_active: bool = True
    # C-07 RED→GREEN：真实因子溯源字段。与 stage_limits_json 中的同名键保持一致（rule 写入时以双写保证）
    factor_set_id: str | None = None
    factor_model_run_id: str | None = None


class AllocationSummary(BaseModel):
    # P1-FIX: 补充资金绝对额字段（总览页可用现金、持仓市值等）
    total_capital: float
    investable_capital: float
    used_amount: float
    cash_amount: float
    investable_remaining_amount: float
    stock_amount: float
    etf_amount: float
    position_count: int
    total_position_pct: float
    stock_position_pct: float
    etf_position_pct: float
    cash_pct: float
    investable_remaining_pct: float
    sector_exposure: dict[str, float]
    sector_amount: dict[str, float]
    remaining_stock_pct: float
    remaining_etf_pct: float


class PositionUpsert(BaseModel):
    symbol_id: int
    quantity: float
    avg_cost: float
    latest_price: float | None = None


class PositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    symbol_id: int
    # P1-FIX: Position 关联 Symbol 返回代码和名称，前端可直接显示识别
    symbol: str | None = None
    name: str | None = None
    quantity: float
    avg_cost: float
    latest_price: float
    market_value: float
    position_pct: float
    asset_type: str
    theme: str | None = None
