"""Strict order-plan-to-trade linkage contracts.

The decision-driven backtest receives the plan at matching time. These tests
deliberately do not look up evidence by symbol or date, because that was the
source of ambiguous links for repeated entries/exits.
"""
from types import SimpleNamespace

import pytest

from app.services.backtest import _apply_order_plan_evidence


def _trade(symbol_id: int = 11):
    return SimpleNamespace(
        symbol_id=symbol_id,
        decision_evidence_id=None,
        exit_evidence_id=None,
        intended_entry_price=None,
        slippage_bps=None,
    )


def test_trade_uses_the_exact_entry_and_exit_plans():
    trade = _trade()
    entry = SimpleNamespace(
        symbol_id=11,
        action="BUY",
        evidence_id="buy-round-2",
        intended_price=10.1,
        slippage_bps=5.0,
    )
    exit_plan = SimpleNamespace(
        symbol_id=11,
        action="SELL",
        evidence_id="sell-round-2",
    )

    _apply_order_plan_evidence(trade, entry_plan=entry, exit_plan=exit_plan)

    assert trade.decision_evidence_id == "buy-round-2"
    assert trade.exit_evidence_id == "sell-round-2"
    assert trade.intended_entry_price == 10.1
    assert trade.slippage_bps == 5.0


def test_repeated_entries_keep_their_distinct_plan_evidence_ids():
    first = _trade()
    second = _trade()
    _apply_order_plan_evidence(
        first,
        entry_plan=SimpleNamespace(
            symbol_id=11, action="BUY", evidence_id="buy-round-1",
            intended_price=10.0, slippage_bps=5.0,
        ),
    )
    _apply_order_plan_evidence(
        second,
        entry_plan=SimpleNamespace(
            symbol_id=11, action="BUY", evidence_id="buy-round-2",
            intended_price=12.0, slippage_bps=5.0,
        ),
    )

    assert first.decision_evidence_id == "buy-round-1"
    assert second.decision_evidence_id == "buy-round-2"


def test_linkage_rejects_symbol_or_action_mismatch():
    with pytest.raises(ValueError, match="symbol"):
        _apply_order_plan_evidence(
            _trade(11),
            entry_plan=SimpleNamespace(
                symbol_id=12, action="BUY", evidence_id="wrong",
                intended_price=10.0, slippage_bps=5.0,
            ),
        )
    with pytest.raises(ValueError, match="SELL"):
        _apply_order_plan_evidence(
            _trade(11),
            exit_plan=SimpleNamespace(symbol_id=11, action="BUY", evidence_id="wrong"),
        )
