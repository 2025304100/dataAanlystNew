"""交易成本模型共享模块。

抽取自 backtest.py 的 _compute_cost + DEFAULT_COST_CONFIG，
供回测、模拟交易、（未来的）自动下单三方共用，避免重复实现和不一致。

设计原则：
- 纯函数，无 DB 依赖，方便单测
- 所有费率字段都通过 config dict 传入，支持组合级配置覆盖
- 兼容 backtest.py 原有调用签名（保留 _compute_cost 别名）
"""
from __future__ import annotations

from typing import Any


# 默认交易成本配置（与 schema 校验默认值保持一致，避免硬编码重复）
# - commission_rate: 券商佣金费率（买卖双向）
# - min_commission: 单笔最低佣金（元）
# - stamp_tax_rate: 印花税税率（仅卖出收取，A 股特色）
# - slippage_rate: 滑点费率（按成交金额比例估算）
DEFAULT_COST_CONFIG: dict[str, float] = {
    "commission_rate": 0.0003,
    "min_commission": 5.0,
    "stamp_tax_rate": 0.001,
    "slippage_rate": 0.001,
}


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
        stamp_tax = amount * cfg["stamp_tax_rate"]
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
