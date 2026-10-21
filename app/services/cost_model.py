"""交易成本模型共享模块。

抽取自 backtest.py 的 _compute_cost + DEFAULT_COST_CONFIG，
供回测、模拟交易、（未来的）自动下单三方共用，避免重复实现和不一致。

设计原则：
- 纯函数，无 DB 依赖，方便单测
- 所有费率字段都通过 config dict 传入，支持组合级配置覆盖
- 兼容 backtest.py 原有调用签名（保留 _compute_cost 别名）
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


# 当前交易成本估算默认值。历史执行不得提前把当前印花税写成显式覆盖；
# 保留缺省/None，由 resolve_stamp_tax_rate 按真实成交日期及品种解析。
# - commission_rate: 券商佣金费率（买卖双向）
# - min_commission: 单笔最低佣金（元）
# - stamp_tax_rate: 印花税税率（仅卖出收取，A 股特色）
# - slippage_rate: 滑点费率（按成交金额比例估算）
DEFAULT_COST_CONFIG: dict[str, float] = {
    "commission_rate": 0.0003,
    "min_commission": 5.0,
    "stamp_tax_rate": 0.0005,
    "slippage_rate": 0.001,
}


def resolve_stamp_tax_rate(
    trade_date: date | datetime | str | None = None,
    *,
    asset_type: str = "stock",
    market: str = "cn",
    override: float | None = None,
) -> float:
    """Resolve an omitted rate by execution date; explicit user rates win.

    The 2023-08-28 reduction applies to sales from that date. Do not resolve
    once at backtest submission: a run can span both sides of the change.
    """
    if override is not None:
        return max(0.0, float(override))
    if asset_type.lower() != "stock" or market.lower() not in {"cn", "sh", "sz", "bj", "sse", "szse", "bse"}:
        return 0.0
    if trade_date is None:
        execution_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    elif isinstance(trade_date, datetime):
        execution_date = trade_date.date()
    elif isinstance(trade_date, str):
        execution_date = date.fromisoformat(trade_date[:10])
    else:
        execution_date = trade_date
    return 0.0005 if execution_date >= date(2023, 8, 28) else 0.001


def normalize_cost_config(config: dict[str, Any] | None) -> dict[str, float]:
    """合并用户传入的 cost config 与默认值，确保字段齐全且为 float。

    防御性处理：
    - None → 返回默认配置的副本
    - 缺字段 → 用默认值补齐
    - 非法值（None/负数/非数值）→ 用默认值兜底
    """
    if config is None:
        return dict(DEFAULT_COST_CONFIG)
    normalized: dict[str, float] = {}
    for key, default_value in DEFAULT_COST_CONFIG.items():
        raw = config.get(key, default_value)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = default_value
        # 费率不能为负（否则倒贴钱），兜底为 0
        if value < 0:
            value = 0.0
        normalized[key] = value
    return normalized


def compute_cost(
    price: float,
    quantity: float,
    side: str,
    config: dict[str, Any] | None = None,
    *,
    trade_date: date | datetime | str | None = None,
    asset_type: str = "stock",
    market: str = "cn",
) -> float:
    """计算单笔交易的综合成本（佣金 + 印花税 + 滑点）。

    Args:
        price: 成交价
        quantity: 成交数量（股）
        side: "buy" 或 "sell"
        config: 成本配置，None 时使用 DEFAULT_COST_CONFIG

    Returns:
        总成本金额（元），保留 2 位小数逻辑由调用方决定

    规则：
    - 佣金 = max(amount * commission_rate, min_commission)，买卖双向
    - 印花税 = amount * stamp_tax_rate，仅卖出收取（A 股规则）
    - 滑点 = amount * slippage_rate，买卖双向（保守估算）
    """
    cfg = normalize_cost_config(config)
    amount = float(price) * float(quantity)

    commission = max(amount * cfg["commission_rate"], cfg["min_commission"])
    slippage = amount * cfg["slippage_rate"]

    if side == "sell":
        raw_config = config or {}
        override = next((raw_config[key] for key in
                         ("stamp_tax_rate", "stamp_duty_rate", "stamp_duty_pct")
                         if raw_config.get(key) is not None), None)
        stamp_tax = amount * resolve_stamp_tax_rate(
            trade_date, asset_type=asset_type, market=market,
            override=override,
        )
        return round(commission + stamp_tax + slippage, 2)

    return round(commission + slippage, 2)


def compute_fill_price_with_slippage(
    base_price: float,
    side: str,
    config: dict[str, Any] | None = None,
) -> float:
    """根据滑点率计算实际成交价。

    买入：成交价 = base_price * (1 + slippage_rate)  （买贵）
    卖出：成交价 = base_price * (1 - slippage_rate)  （卖便宜）

    用于模拟交易时把"最新收盘价"调整为更真实的成交价。
    """
    cfg = normalize_cost_config(config)
    rate = cfg["slippage_rate"]
    if side == "buy":
        return round(float(base_price) * (1.0 + rate), 4)
    if side == "sell":
        return round(float(base_price) * (1.0 - rate), 4)
    return float(base_price)


# 向后兼容别名：backtest.py 原有的 _compute_cost 签名
# 保持 (price, quantity, side, config) → float 的调用不变
_compute_cost = compute_cost
