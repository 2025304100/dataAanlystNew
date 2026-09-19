"""退市强制清算状态机。

按需求文档 §4.4 执行：
- 摘牌日用最后一个有效收盘价（非空且 > 0，从 DailyBar 取）强制平仓；
- 盈亏计入净值流水（由调用方写入 BacktestExecutionFill）；
- 最后交易日无有效收盘价：抛 DelistingPriceMissingError，回测阻断；
- 退市后的历史数据不删除（不在此处处理）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping, Sequence

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.services.backtest_filters.rules import (
    ACTION_FORCE_LIQUIDATE,
    DelistingCandidate,
    DelistingPriceMissingError,
    FilterEventDTO,
    RULE_DELISTING_LIQUIDATION_MANDATORY,
    RULE_DELISTING_PRICE_MISSING,
)
from app.services.security_status.pit_service import SecurityStatusDTO

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HoldingInfo:
    """持仓信息（清算输入）。"""
    quantity: float
    avg_cost_price: float = 0.0


@dataclass(frozen=True)
class LiquidationResult:
    """一笔退市清算的结果。"""
    run_id: int | None
    symbol_id: int
    trade_date: date
    quantity: float               # 清算股数（=持仓），正数
    exit_price: float             # 清算价（最后有效收盘价）
    pnl: float                    # 盈亏 = (exit_price - avg_cost_price) × quantity
    avg_cost_price: float         # 持仓平均成本（用于净值计算方参考）
    event_code: str               # RULE_DELISTING_LIQUIDATION_MANDATORY


# ---------------------------------------------------------------------------
# 价格解析器默认实现：从 DailyBar 取 trade_date 之前（含）最后一个 close > 0
# ---------------------------------------------------------------------------
def _default_price_resolver(
    db: Session,
    symbol_id: int,
    trade_date: date,
) -> float | None:
    """默认价格解析：从 DailyBar 取 max(trade_date <= target) 且 close>0。"""
    from app.models.daily_bar import DailyBar
    row = db.execute(
        select(DailyBar.close)
        .where(
            DailyBar.symbol_id == int(symbol_id),
            DailyBar.trade_date <= trade_date,
            DailyBar.close.is_not(None),
            DailyBar.close > 0,
        )
        .order_by(DailyBar.trade_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    close = float(row)
    # 最后防线：NaN 视为无效（SQL 层 close>0 已排除 0 和负数）
    if close != close:  # NaN self-inequality
        return None
    return close


def process_delisting_liquidations(
    db: Session,
    run_id: int | None,
    trade_date: date,
    positions: Mapping[int, "HoldingInfo"],
    pit_status_map: Mapping[int, SecurityStatusDTO],
    delisting_candidates: Sequence[DelistingCandidate],
    price_resolver: Callable[[Session, int, date], float | None] | None = None,
    config_hash: str = "",
    data_batch_id: str | None = None,
) -> tuple[list[LiquidationResult], list[FilterEventDTO]]:
    """对所有 DELISTED 的持仓执行强制清算。

    Args:
        positions: {symbol_id: HoldingInfo(quantity, avg_cost_price)}。
        pit_status_map: 与 apply_daily_filters 传入同一 PIT 状态。
        delisting_candidates: apply_daily_filters 输出的退市清算候选列表（仅含已 DELISTED 且 qty>0）。
        price_resolver: 可注入以方便测试；None 时使用默认的 DailyBar 最后有效 close。

    Returns:
        (liquidation_results, extra_audit_events_with_price)

    Raises:
        DelistingPriceMissingError: 任一候选缺收盘价时整体抛出（不部分清算半持仓）。
    """
    resolve = price_resolver or _default_price_resolver
    candidates = list(delisting_candidates)
    results: list[LiquidationResult] = []
    extra_events: list[FilterEventDTO] = []

    # 预检查：所有候选必须有价格（否则阻断，不部分清算）
    resolved_prices: dict[int, float] = {}
    for cand in candidates:
        # 如果候选本身已提供 last_valid_close（且非 None/NaN/≤0），优先使用
        if cand.last_valid_close is not None:
            p = float(cand.last_valid_close)
            if p == p and p > 0:
                resolved_prices[cand.symbol_id] = p
                continue
        p = resolve(db, cand.symbol_id, trade_date)
        if p is None:
            raise DelistingPriceMissingError(
                symbol_id=cand.symbol_id,
                trade_date=trade_date,
                evidence={
                    "resolver_used": getattr(resolve, "__name__", "custom"),
                    "candidates_total": len(candidates),
                },
            )
        resolved_prices[cand.symbol_id] = p

    # 执行清算
    for cand in candidates:
        sid = cand.symbol_id
        price = resolved_prices[sid]
        holding = positions.get(sid)
        if holding is None:
            continue
        qty = float(holding.quantity)
        if qty <= 0:
            continue
        avg_cost = float(holding.avg_cost_price)
        pnl = (price - avg_cost) * qty

        res = LiquidationResult(
            run_id=run_id,
            symbol_id=sid,
            trade_date=trade_date,
            quantity=qty,
            exit_price=price,
            pnl=pnl,
            avg_cost_price=avg_cost,
            event_code=RULE_DELISTING_LIQUIDATION_MANDATORY,
        )
        results.append(res)

        # 额外事件：回填 price_used（把 engine 的 FORCE_LIQUIDATE 事件价格填实，或新增一条）
        # 这里新产生一条以 price_used 已填实的事件，调用方可合并写入审计
        extra_events.append(FilterEventDTO(
            run_id=run_id,
            trade_date=trade_date,
            symbol_id=sid,
            action=ACTION_FORCE_LIQUIDATE,
            rule_code=RULE_DELISTING_LIQUIDATION_MANDATORY,
            reason=f"退市清算执行：price={price:.4f}, qty={qty}, pnl={pnl:.4f}",
            raw_status_json="{}",
            effective_status=(pit_status_map[sid].status if sid in pit_status_map else "DELISTED"),
            price_used=price,
            config_hash=config_hash,
            data_batch_id=data_batch_id,
        ))

    return results, extra_events
