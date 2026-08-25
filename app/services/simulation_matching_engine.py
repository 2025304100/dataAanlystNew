"""WP0-5 / G2-2 统一撮合服务（回测 + auto_simulation 两条链路共用，不得各自实现）。

对外暴露三个核心：
  - match_order_plan()   : 涨跌停/停牌/流动性锁/滑点/价格类型 → 成交或拒单
  - apply_cost_model()   : 手续费+印花税+过户费统一计算
  - update_portfolio_state() : 持仓/现金/未成交订单守恒更新

撮合语义（与 WP0-5 TR-05.9 对齐）：
  - NEXT_OPEN 为默认正式口径；T_CLOSE 仅允许 research preflight（带 override 标记）
  - 风险退出（强制止损/清仓类 SELL）优先级高于普通 BUY，同 session 内先处理风险卖单
  - 涨跌停 / 停牌无法成交：final_status = REJECTED_TRADE_HALTED，
    reason_codes = [PRICE_LIMIT_LOCKED_*_SIDE, CANNOT_FILL_AT_NEXT_OPEN]，
    生成"待处理交易计划"（OrderPlan.pending），下一交易日继续检查；
    信号过期前达到可成交条件执行；过期后 SIGNAL_EXPIRED 关闭。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

MatchPriceType = Literal["NEXT_OPEN", "T_CLOSE"]
FinalStatus = Literal[
    "FILLED",
    "PARTIAL_FILL",
    "REJECTED_TRADE_HALTED",
    "REJECTED_BELOW_LOT",
    "REJECTED_NO_QUANTITY",
    "SIGNAL_EXPIRED",
    "PENDING_RETRY",
]


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


# ──────────────────────────────────────────────────────────── 输入/输出 DTO
@dataclass
class OrderPlan:
    order_plan_id: str
    symbol_id: int
    portfolio_id: int
    trade_date: date
    price_type: MatchPriceType
    side: OrderSide
    target_quantity: int  # 正数
    min_lot_size: int = 100
    intended_price: float | None = None  # 决策时参考价（方向决定上/下行滑点）
    signal_expire_date: date | None = None  # None=永不失效（如风险止损）
    is_risk_exit: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketBar:
    """NEXT_OPEN 用当日 open；T_CLOSE 用当日 close。涨跌停用 pre_close × ±10%/±20%。"""
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    pre_close: float | None = None
    volume: float | None = None
    amount: float | None = None
    halted: bool = False  # 停牌
    upper_limit_price: float | None = None
    lower_limit_price: float | None = None
    limit_up_locked: bool = False  # 开盘封涨停，买侧无法入场
    limit_down_locked: bool = False  # 开盘封跌停，卖侧无法离场


@dataclass
class MatchResult:
    order_plan_id: str
    final_status: FinalStatus
    filled_quantity: int
    executed_price: float | None
    slippage_bps: float | None
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    total_cost: float = 0.0
    reason_codes: list[str] = field(default_factory=list)
    retry_on_next_session: bool = False
    note: str | None = None


@dataclass
class CostModelConfig:
    commission_rate: float = 0.0003   # 万3
    min_commission: float = 5.0       # A 股最低 5 元
    stamp_tax_rate: float = 0.001     # 卖出单边千1
    transfer_fee_rate: float = 0.00001  # 过户费（双边），默认 0.001%
    slippage_buy_bps: int = 5         # 买侧滑点 bps
    slippage_sell_bps: int = 5        # 卖侧滑点 bps
    # 单笔订单最多占当日成交量的比例；None 表示不限制。
    volume_limit_pct: float | None = None
    # 涨跌停幅度（A 股主板 ±10%；科创板/创业板由调用方通过 bar 传入）
    default_limit_up_pct: float = 0.10
    default_limit_down_pct: float = 0.10


# ──────────────────────────────────────────────────────────── 1. 撮合
def _resolve_limit_prices(bar: MarketBar, cfg: CostModelConfig) -> tuple[float | None, float | None]:
    up = bar.upper_limit_price
    dn = bar.lower_limit_price
    if up is None and bar.pre_close:
        up = round(float(bar.pre_close) * (1.0 + cfg.default_limit_up_pct), 4)
    if dn is None and bar.pre_close:
        dn = round(float(bar.pre_close) * (1.0 - cfg.default_limit_down_pct), 4)
    return up, dn


def _pick_raw_price(bar: MarketBar, price_type: MatchPriceType) -> float | None:
    if price_type == "NEXT_OPEN":
        return bar.open
    # T_CLOSE（仅研究模式）
    return bar.close


def match_order_plan(
    plan: OrderPlan,
    bar: MarketBar,
    *,
    price_type_override: MatchPriceType | None = None,
    cfg: CostModelConfig | None = None,
) -> MatchResult:
    """统一撮合：相同输入 → 相同输出（保证回测/仿真两条链路 hash 一致）。"""
    cfg = cfg or CostModelConfig()
    effective_price_type: MatchPriceType = price_type_override or plan.price_type

    base_reasons: list[str] = []
    # ── 前置拒绝：最小手数 / 数量为 0
    if plan.target_quantity <= 0:
        return MatchResult(
            order_plan_id=plan.order_plan_id,
            final_status="REJECTED_NO_QUANTITY",
            filled_quantity=0,
            executed_price=None,
            slippage_bps=None,
            reason_codes=["NO_QUANTITY"],
            note="target_quantity <= 0",
        )
    if plan.target_quantity % plan.min_lot_size != 0:
        return MatchResult(
            order_plan_id=plan.order_plan_id,
            final_status="REJECTED_BELOW_LOT",
            filled_quantity=0,
            executed_price=None,
            slippage_bps=None,
            reason_codes=["ORDER_BELOW_LOT_SIZE"],
            note=(
                f"quantity={plan.target_quantity} not multiple of "
                f"min_lot_size={plan.min_lot_size}"
            ),
        )

    # ── 信号过期
    if plan.signal_expire_date is not None and bar.trade_date > plan.signal_expire_date:
        return MatchResult(
            order_plan_id=plan.order_plan_id,
            final_status="SIGNAL_EXPIRED",
            filled_quantity=0,
            executed_price=None,
            slippage_bps=None,
            reason_codes=["SIGNAL_EXPIRED"],
            retry_on_next_session=False,
            note=f"bar.trade_date={bar.trade_date} > expire={plan.signal_expire_date}",
        )

    # ── 停牌 → 待处理
    if bar.halted:
        return MatchResult(
            order_plan_id=plan.order_plan_id,
            final_status="REJECTED_TRADE_HALTED",
            filled_quantity=0,
            executed_price=None,
            slippage_bps=None,
            reason_codes=["STOCK_HALTED", "CANNOT_FILL_AT_NEXT_OPEN"],
            retry_on_next_session=True,
        )

    up, dn = _resolve_limit_prices(bar, cfg)
    raw_price = _pick_raw_price(bar, effective_price_type)
    if raw_price is None or raw_price <= 0:
        base_reasons.append("NO_VALID_BAR_PRICE")
        return MatchResult(
            order_plan_id=plan.order_plan_id,
            final_status="REJECTED_TRADE_HALTED",
            filled_quantity=0,
            executed_price=None,
            slippage_bps=None,
            reason_codes=base_reasons + ["CANNOT_FILL_AT_NEXT_OPEN"],
            retry_on_next_session=True,
        )

    # ── 涨跌停锁（正式链路严格，风险退出也接受以涨停价/跌停价成交若达到）
    buy_side = plan.side is OrderSide.BUY
    if buy_side and bar.limit_up_locked:
        return MatchResult(
            order_plan_id=plan.order_plan_id,
            final_status="REJECTED_TRADE_HALTED",
            filled_quantity=0,
            executed_price=None,
            slippage_bps=None,
            reason_codes=["PRICE_LIMIT_LOCKED_BUY_SIDE", "CANNOT_FILL_AT_NEXT_OPEN"],
            retry_on_next_session=True,
        )
    if not buy_side and bar.limit_down_locked:
        return MatchResult(
            order_plan_id=plan.order_plan_id,
            final_status="REJECTED_TRADE_HALTED",
            filled_quantity=0,
            executed_price=None,
            slippage_bps=None,
            reason_codes=["PRICE_LIMIT_LOCKED_SELL_SIDE", "CANNOT_FILL_AT_NEXT_OPEN"],
            retry_on_next_session=True,
        )
    if buy_side and up is not None and raw_price >= up:
        # 开盘价就等于涨停价且不是封板→允许成交但使用涨停价（上限）
        raw_price = up
    if not buy_side and dn is not None and raw_price <= dn:
        raw_price = dn

    # ── 方向性滑点（NEXT_OPEN 默认 directional）
    if buy_side:
        s_bps = float(cfg.slippage_buy_bps)
        executed_price = raw_price * (1.0 + s_bps / 10_000.0)
    else:
        s_bps = float(cfg.slippage_sell_bps)
        executed_price = raw_price * (1.0 - s_bps / 10_000.0)

    executed_price = round(float(executed_price), 6)
    filled_qty = int(plan.target_quantity)
    partial_fill = False
    if cfg.volume_limit_pct is not None and bar.volume is not None and float(bar.volume) > 0:
        limit_qty = int(math.floor(float(bar.volume) * float(cfg.volume_limit_pct)))
        limit_qty = (limit_qty // int(plan.min_lot_size)) * int(plan.min_lot_size)
        if limit_qty <= 0:
            return MatchResult(
                order_plan_id=plan.order_plan_id,
                final_status="REJECTED_BELOW_LOT",
                filled_quantity=0,
                executed_price=None,
                slippage_bps=None,
                reason_codes=["VOLUME_LIMIT", "ORDER_BELOW_LOT_SIZE"],
                retry_on_next_session=True,
                note=f"volume={bar.volume}, volume_limit_pct={cfg.volume_limit_pct}",
            )
        if filled_qty > limit_qty:
            filled_qty = limit_qty
            partial_fill = True

    # ── 成本计算
    cost = apply_cost_model(
        plan.side,
        filled_qty,
        executed_price,
        cfg=cfg,
    )
    return MatchResult(
        order_plan_id=plan.order_plan_id,
        final_status="PARTIAL_FILL" if partial_fill else "FILLED",
        filled_quantity=filled_qty,
        executed_price=executed_price,
        slippage_bps=s_bps,
        commission=cost["commission"],
        stamp_tax=cost["stamp_tax"],
        transfer_fee=cost["transfer_fee"],
        total_cost=cost["total_cost"],
        reason_codes=base_reasons + (["PARTIAL_FILL"] if partial_fill else []),
        retry_on_next_session=partial_fill,
        note=(f"requested={plan.target_quantity}, filled={filled_qty}" if partial_fill else None),
    )


# ──────────────────────────────────────────────────────────── 2. 成本模型
def apply_cost_model(
    side: OrderSide,
    quantity: int,
    price: float,
    *,
    cfg: CostModelConfig | None = None,
) -> dict[str, float]:
    """统一成本：手续费+印花税（卖单）+过户费（双边）。"""
    cfg = cfg or CostModelConfig()
    if quantity <= 0 or price <= 0:
        return {"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "total_cost": 0.0}
    gross = float(quantity) * float(price)
    commission_raw = gross * float(cfg.commission_rate)
    commission = float(max(cfg.min_commission, commission_raw) if cfg.min_commission > 0 else commission_raw)
    stamp_tax = 0.0
    if side is OrderSide.SELL:
        stamp_tax = gross * float(cfg.stamp_tax_rate)
    transfer_fee = gross * float(cfg.transfer_fee_rate)
    total = round(commission + stamp_tax + transfer_fee, 8)
    return {
        "commission": round(commission, 6),
        "stamp_tax": round(stamp_tax, 6),
        "transfer_fee": round(transfer_fee, 6),
        "total_cost": round(total, 6),
    }


# ──────────────────────────────────────────────────────────── 3. 组合持仓/现金守恒更新
@dataclass
class PortfolioState:
    cash: float = 0.0
    holdings: dict[int, int] = field(default_factory=dict)  # symbol_id -> qty
    pending_orders: dict[str, OrderPlan] = field(default_factory=dict)

    def copy(self) -> "PortfolioState":
        return PortfolioState(
            cash=float(self.cash),
            holdings=dict(self.holdings),
            pending_orders=dict(self.pending_orders),
        )


def update_portfolio_state(
    state: PortfolioState,
    plan: OrderPlan,
    result: MatchResult,
) -> PortfolioState:
    """保持现金/持仓守恒；未成交且需要重试的挂入 pending_orders。"""
    next_state = state.copy()
    qty = int(result.filled_quantity)
    if qty > 0 and result.executed_price is not None:
        executed_price = float(result.executed_price)
        gross = float(qty) * executed_price
        cost = float(result.total_cost)
        if plan.side is OrderSide.BUY:
            next_state.cash -= (gross + cost)
            next_state.holdings[plan.symbol_id] = int(next_state.holdings.get(plan.symbol_id, 0)) + qty
        else:
            next_state.cash += (gross - cost)
            next_state.holdings[plan.symbol_id] = int(next_state.holdings.get(plan.symbol_id, 0)) - qty
            if next_state.holdings[plan.symbol_id] <= 0:
                next_state.holdings.pop(plan.symbol_id, None)
    if result.retry_on_next_session and result.final_status != "FILLED":
        next_state.pending_orders[plan.order_plan_id] = plan
    else:
        next_state.pending_orders.pop(plan.order_plan_id, None)
    # 现金向下取整 2 位（避免浮点噪声）
    next_state.cash = round(float(next_state.cash), 2)
    return next_state


# ──────────────────────────────────────────────────────────── 4. 风险退出优先级排序
def sort_plans_by_risk_priority(plans: list[OrderPlan]) -> list[OrderPlan]:
    """先执行风险退出卖单 → 其他卖单 → 买单；稳定排序，保持同组原顺序。"""
    def _key(p: OrderPlan) -> tuple[int, int]:
        risk_first = 0 if p.is_risk_exit else 1
        side_order = 0 if p.side is OrderSide.SELL else 1
        return (risk_first, side_order)
    return sorted(plans, key=_key)
