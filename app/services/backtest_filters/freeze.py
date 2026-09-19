"""停牌持仓冻结契约与撮合前 skip_set 生成（纯函数 + 对账工具）。

撮合链路需要保证：
- frozen symbol_ids 在 match_engine.execute() 之前被加入 skip_set，完全跳过买入/卖出；
- 当日 BacktestPosition 中冻结持仓 opening_quantity == closing_quantity；buy_quantity = sell_quantity = 0；
- BacktestExecutionFill 当日冻结持仓条目数 = 0；
- 所有冻结持仓对应一条 SUSPENDED_FREEZE 审计事件已由 apply_daily_filters 产出。

代码接入指南（在 Task 16 中实现）：在 app/services/backtest.py 主循环内，生成
  candidate_pool 之后、撮合之前插入：

  frozen_skips = frozen_symbols_skip_set(filter_outcome.frozen_positions)
  # 透传给 match_engine.execute(skip_symbol_ids=frozen_skips) 或在调仓循环前先 remove 掉 frozen 的订单
  matcher_result = match_engine.execute(orders, skip_symbol_ids=frozen_skips)

  # 撮合后校验契约（开发/调试模式）：
  if __debug__:
      viols = validate_freeze_contract(trade_date=td, frozen_ids=frozen_skips, ...)
      assert not viols, f"FREEZE CONTRACT VIOLATED: {viols}"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from app.services.backtest_filters.rules import FrozenPositionInfo, FilterEventDTO

logger = logging.getLogger(__name__)


def frozen_symbols_skip_set(
    frozen_positions: Mapping[int, FrozenPositionInfo] | Iterable[int],
) -> set[int]:
    """从 apply_daily_filters 输出的 frozen_positions 构造撮合 skip_set。

    Args:
        frozen_positions: 支持两种形式：
            1) dict[int, FrozenPositionInfo] — 过滤器直接输出
            2) set/list[int] — 调用方直接传 symbol_id 集合

    Returns:
        set[int] symbol_ids，撮合引擎对这些 symbol 跳过任何买卖指令。
    """
    if isinstance(frozen_positions, Mapping):
        return {int(sid) for sid in frozen_positions.keys()}
    return {int(sid) for sid in frozen_positions}


# ---------------------------------------------------------------------------
# 持仓冻结契约验证（调试/断言用，确保撮合的冻结效果正确）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FreezeContractViolation:
    symbol_id: int
    trade_date: date
    violation_code: str
    detail: str


def validate_freeze_contract(
    *,
    trade_date: date,
    frozen_ids: set[int],
    opening_by_symbol: Mapping[int, float],     # 当日开盘持仓
    fills_symbol_ids: Iterable[int],           # 当日所有执行成交的 symbol_id 集合（去重即可，来自 BacktestExecutionFill.symbol_id）
    buy_qty_by_symbol: Mapping[int, float],     # 当日买入量
    sell_qty_by_symbol: Mapping[int, float],    # 当日卖出量
    closing_by_symbol: Mapping[int, float],     # 当日收盘持仓
    filter_events: Iterable[FilterEventDTO] | None = None,
) -> list[FreezeContractViolation]:
    """返回所有违反冻结契约的条目（空列表表示通过）。

    验证规则（每个 frozen symbol）：
    V1. closing == opening（若 opening 提供则必须相等）
    V2. buy_qty == 0 且 sell_qty == 0
    V3. 不在当日成交 symbol_ids 中（fill_count=0）
    V4. filter_events 中有一条 SUSPENDED_FREEZE（若传入 filter_events）
    """
    violations: list[FreezeContractViolation] = []

    # V4：检查 SUSPENDED_FREEZE 事件存在
    freeze_event_sids: set[int] = set()
    if filter_events is not None:
        from app.services.backtest_filters.rules import RULE_SUSPENDED_FREEZE, ACTION_FREEZE
        for ev in filter_events:
            if ev.rule_code == RULE_SUSPENDED_FREEZE and ev.action == ACTION_FREEZE and ev.trade_date == trade_date:
                freeze_event_sids.add(int(ev.symbol_id))

    for sid in sorted(frozen_ids):
        # V1: opening == closing（若任一方未提供，跳过该验证）
        if sid in opening_by_symbol and sid in closing_by_symbol:
            if opening_by_symbol[sid] != closing_by_symbol[sid]:
                violations.append(FreezeContractViolation(
                    symbol_id=sid, trade_date=trade_date,
                    violation_code="FREEZE_V1_CLOSING_MISMATCH",
                    detail=f"opening={opening_by_symbol[sid]} != closing={closing_by_symbol[sid]}（冻结持仓应不变）",
                ))
        # V2: buy/sell 均为 0
        buy = buy_qty_by_symbol.get(sid, 0.0)
        sell = sell_qty_by_symbol.get(sid, 0.0)
        if buy != 0.0 or sell != 0.0:
            violations.append(FreezeContractViolation(
                symbol_id=sid, trade_date=trade_date,
                violation_code="FREEZE_V2_NONZERO_TRADE_QTY",
                detail=f"buy_qty={buy}, sell_qty={sell}（冻结持仓成交量必须为 0）",
            ))
        # V3: 不在 fills 中
        fills_set = {int(x) for x in fills_symbol_ids}
        if sid in fills_set:
            violations.append(FreezeContractViolation(
                symbol_id=sid, trade_date=trade_date,
                violation_code="FREEZE_V3_FILLS_EXIST",
                detail="冻结持仓出现在 BacktestExecutionFill 中（冻结日不得有成交）",
            ))
        # V4: 有冻结事件
        if filter_events is not None and sid not in freeze_event_sids:
            violations.append(FreezeContractViolation(
                symbol_id=sid, trade_date=trade_date,
                violation_code="FREEZE_V4_NO_AUDIT_EVENT",
                detail="冻结持仓缺少对应的 SUSPENDED_FREEZE 审计事件",
            ))
    return violations
