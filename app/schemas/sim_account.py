from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SimOrderCreate(BaseModel):
    symbol_id: int
    side: str
    quantity: float = Field(gt=0)
    price: float | None = Field(default=None, gt=0)
    order_type: str = "market"
    note: str | None = None
    # 改造后新增：可选控制市场规则与成本模型
    # None → 使用后端默认（True），显式传 False 可禁用（用于测试/兼容旧版前端）
    enforce_rules: bool | None = None
    apply_fees: bool | None = None
    cost_config: dict[str, Any] | None = None


class SimOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    symbol_id: int
    side: str
    order_type: str
    quantity: float
    submitted_price: float
    status: str
    filled_quantity: float
    filled_price: float
    filled_amount: float
    fee: float
    note: str | None = None
    created_at: datetime
    filled_at: datetime | None = None


class SimTradeRead(BaseModel):
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


class CashLedgerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    entry_type: str
    amount: float
    balance_after: float
    ref_type: str | None = None
    ref_id: int | None = None
    note: str | None = None
    created_at: datetime


class SimAccountSummary(BaseModel):
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


class SimAccountSnapshot(BaseModel):
    summary: SimAccountSummary
    recent_trades: list[SimTradeRead]
    recent_ledger: list[CashLedgerRead]


class SimOrderExecutionResult(BaseModel):
    order: SimOrderRead
    trade: SimTradeRead
    summary: SimAccountSummary
