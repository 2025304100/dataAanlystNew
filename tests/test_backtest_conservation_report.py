"""Contracts for the read-only backtest conservation report."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.reconciliation_conservation import evaluate_five_vector_conservation
from scripts.backtest_conservation_report import _conservation_price, _is_actionable_side, build_report


def test_legacy_run_is_explicitly_excluded_from_modern_daily_report():
    run = SimpleNamespace(
        id=7,
        run_name="legacy",
        strategy_snapshot_id=None,
        reproducibility_status="legacy/non_reproducible",
        reproducibility_reason=None,
    )

    report = build_report(SimpleNamespace(), run)

    assert report["reproducibility_status"] == "legacy/non_reproducible"
    assert report["daily"] == []
    assert "未绑定" in report["reason"]


def test_fee_adjusted_price_keeps_cash_conservation_exact():
    fill = SimpleNamespace(quantity=100, executed_price=10.0, cost=2.0, side="BUY")
    effective = _conservation_price(fill)
    result = evaluate_five_vector_conservation({
        "trade_date": __import__("datetime").date(2026, 8, 22),
        "start_cash": 1_000.0,
        "end_cash": 1_000.0 - 100 * 10.0 - 2.0,
        "start_positions": {},
        "end_positions": {1: 100},
        "end_prices": {1: 10.0},
        "decisions": [{"symbol_id": 1, "action": "BUY", "target_qty": 100}],
        "order_plans": [{"order_plan_id": "p1", "symbol_id": 1, "side": "BUY", "plan_qty": 100}],
        "match_results": [{
            "order_plan_id": "p1", "symbol_id": 1, "side": "BUY",
            "status": "FILLED", "filled_qty": 100, "avg_price": effective,
        }],
    })

    assert result.overall == "PASSED"


def test_non_actionable_evidence_is_not_converted_to_matcher_order():
    assert _is_actionable_side("BUY") is True
    assert _is_actionable_side("SELL") is True
    assert _is_actionable_side("HOLD") is False
    assert _is_actionable_side("NO_ACTION") is False
