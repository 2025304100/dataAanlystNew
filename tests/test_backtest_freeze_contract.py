"""停牌冻结契约工具单元测试（8 个用例，覆盖 skip_set + V1~V4 + 全通过 + 无关 symbol 不受影响）。"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.backtest_filters.freeze import (
    FreezeContractViolation,
    frozen_symbols_skip_set,
    validate_freeze_contract,
)
from app.services.backtest_filters.rules import (
    ACTION_FREEZE,
    FilterEventDTO,
    FrozenPositionInfo,
    RULE_SUSPENDED_FREEZE,
)

TRADE_DATE = date(2024, 6, 1)


def _freeze_info(sid: int, qty: float = 100.0) -> FrozenPositionInfo:
    return FrozenPositionInfo(
        symbol_id=sid,
        reason_rule_code=RULE_SUSPENDED_FREEZE,
        reason_detail="停牌持仓冻结",
        opening_quantity=qty,
    )


def _freeze_event(sid: int, td: date = TRADE_DATE) -> FilterEventDTO:
    return FilterEventDTO(
        trade_date=td,
        symbol_id=sid,
        action=ACTION_FREEZE,
        rule_code=RULE_SUSPENDED_FREEZE,
        reason="停牌冻结",
    )


# =====================================================================
# skip_set 生成
# =====================================================================
def test_skip_set_from_mapping():
    frozen = {1: _freeze_info(1), 2: _freeze_info(2)}
    result = frozen_symbols_skip_set(frozen)
    assert result == {1, 2}


def test_skip_set_from_iterable():
    result = frozen_symbols_skip_set([1, 2, 3])
    assert result == {1, 2, 3}
    # set 入参也 OK
    assert frozen_symbols_skip_set({4, 5}) == {4, 5}


# =====================================================================
# V1: opening != closing → 违规
# =====================================================================
def test_contract_v1_opening_closing_mismatch_reports():
    viols = validate_freeze_contract(
        trade_date=TRADE_DATE,
        frozen_ids={3},
        opening_by_symbol={3: 100.0},
        fills_symbol_ids=[],
        buy_qty_by_symbol={},
        sell_qty_by_symbol={},
        closing_by_symbol={3: 80.0},
    )
    assert len(viols) == 1
    assert viols[0].violation_code == "FREEZE_V1_CLOSING_MISMATCH"
    assert viols[0].symbol_id == 3


# =====================================================================
# V2: buy/sell 非零 → 违规
# =====================================================================
def test_contract_v2_nonzero_buy_sell():
    # 仅 buy 非零
    viols_buy = validate_freeze_contract(
        trade_date=TRADE_DATE,
        frozen_ids={4},
        opening_by_symbol={4: 0.0},
        fills_symbol_ids=[],
        buy_qty_by_symbol={4: 10.0},
        sell_qty_by_symbol={},
        closing_by_symbol={4: 10.0},
    )
    assert any(v.violation_code == "FREEZE_V2_NONZERO_TRADE_QTY" for v in viols_buy)

    # 仅 sell 非零
    viols_sell = validate_freeze_contract(
        trade_date=TRADE_DATE,
        frozen_ids={5},
        opening_by_symbol={5: 100.0},
        fills_symbol_ids=[],
        buy_qty_by_symbol={},
        sell_qty_by_symbol={5: 20.0},
        closing_by_symbol={5: 80.0},
    )
    assert any(v.violation_code == "FREEZE_V2_NONZERO_TRADE_QTY" for v in viols_sell)


# =====================================================================
# V3: frozen sid 出现在 fills → 违规
# =====================================================================
def test_contract_v3_in_fills_set():
    viols = validate_freeze_contract(
        trade_date=TRADE_DATE,
        frozen_ids={6},
        opening_by_symbol={6: 100.0},
        fills_symbol_ids=[6, 7],
        buy_qty_by_symbol={},
        sell_qty_by_symbol={},
        closing_by_symbol={6: 100.0},
    )
    assert len(viols) == 1
    assert viols[0].violation_code == "FREEZE_V3_FILLS_EXIST"
    assert viols[0].symbol_id == 6


# =====================================================================
# V4: filter_events 中无 SUSPENDED_FREEZE → 违规
# =====================================================================
def test_contract_v4_missing_event():
    # 给 filter_events 但不含 7 的冻结事件
    events = [_freeze_event(8)]
    viols = validate_freeze_contract(
        trade_date=TRADE_DATE,
        frozen_ids={7},
        opening_by_symbol={7: 100.0},
        fills_symbol_ids=[],
        buy_qty_by_symbol={},
        sell_qty_by_symbol={},
        closing_by_symbol={7: 100.0},
        filter_events=events,
    )
    assert len(viols) == 1
    assert viols[0].violation_code == "FREEZE_V4_NO_AUDIT_EVENT"
    assert viols[0].symbol_id == 7


# =====================================================================
# 全通过：opening==closing，buy/sell=0，fills 无，events 有
# =====================================================================
def test_contract_all_pass():
    events = [_freeze_event(9), _freeze_event(10)]
    viols = validate_freeze_contract(
        trade_date=TRADE_DATE,
        frozen_ids={9, 10},
        opening_by_symbol={9: 100.0, 10: 200.0},
        fills_symbol_ids=[11, 12],   # 非 frozen 的 fills 不影响
        buy_qty_by_symbol={11: 50.0},
        sell_qty_by_symbol={12: 10.0},
        closing_by_symbol={9: 100.0, 10: 200.0},
        filter_events=events,
    )
    assert viols == []


# =====================================================================
# 非冻结 symbol 有买卖，不影响 frozen 的契约
# =====================================================================
def test_contract_irrelevant_symbols_unaffected():
    # frozen={20} 全部正常；非 frozen 的 21、22 有成交 + 持仓变化
    viols = validate_freeze_contract(
        trade_date=TRADE_DATE,
        frozen_ids={20},
        opening_by_symbol={20: 50.0, 21: 30.0, 22: 10.0},
        fills_symbol_ids=[21, 22],
        buy_qty_by_symbol={21: 15.0, 22: 0.0},
        sell_qty_by_symbol={21: 0.0, 22: 5.0},
        closing_by_symbol={20: 50.0, 21: 45.0, 22: 5.0},
        filter_events=[_freeze_event(20)],
    )
    assert viols == []
