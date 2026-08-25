from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.decision_engine import (
    DecisionEngine,
    DecisionStateContext,
    LoadedSnapshot,
    PerSymbolEvidence,
    RiskAllocationResult,
    ScoredUniverse,
    SignalResult,
    UniverseAndEligibility,
)


def _engine_with_observer(observed: dict):
    snapshot = SimpleNamespace(
        id="snapshot-plan-seam",
        portfolio_id=1,
        factor_model_run_id="model-plan-seam",
        factor_set_id=None,
        rule_id=None,
        rule_version=None,
        snapshot_hash="hash-plan-seam",
        snapshot_type="save_and_apply",
    )
    loaded = LoadedSnapshot(
        snapshot=snapshot,
        factor_model_run_id="model-plan-seam",
        factor_set_id=None,
        rule_id=None,
        rule_version=None,
        members=[{"symbol_id": 7}],
        versions={"run_mode": "research", "pit_mode": "best_effort"},
    )

    def load_snapshot(_db, _snapshot_id):
        return loaded

    def gate(_db, _snap, _clock):
        return True, [], {"passed": True}

    def build_universe(_db, _snap, _cutoff):
        return UniverseAndEligibility(universe=[{"symbol_id": 7}], universe_count=1, member_count=1)

    def health(_db, universe, _cutoff):
        return universe

    def score(_db, _snap, _universe, _cutoff, _trade_date):
        return ScoredUniverse(expected=1, actual=1, coverage_pct=100.0, items=[{
            "symbol_id": 7,
            "quality_score": 0.9,
            "score_id": 9,
            "published_at": None,
            "trade_date": date(2025, 1, 6),
        }])

    def signal(_db, _snap, _scored):
        return SignalResult(items=[{"symbol_id": 7, "direction": "BUY"}])

    def allocator(_db, snap, _signal, _cutoff):
        observed["override"] = snap.allocation_state_override
        observed["price_hints"] = snap.price_hints
        return RiskAllocationResult(items=[{
            "symbol_id": 7,
            "direction": "BUY",
            "target_position_pct": 0.1,
            "target_quantity": 100.0,
            "min_lot_size": 100,
            "intended_price": 10.0,
            "executed_price": 10.005,
            "slippage_bps": 5.0,
            "clamp_steps": [],
        }])

    def evidence_builder(**_kwargs):
        return [PerSymbolEvidence(
            symbol_id=7,
            action="BUY",
            target_quantity=100.0,
            target_qty_delta=100.0,
            intended_price=10.0,
        )]

    return DecisionEngine(
        snapshot_loader=load_snapshot,
        gate=gate,
        universe_builder=build_universe,
        health=health,
        scorer=score,
        signal=signal,
        allocator=allocator,
        evidence_builder=evidence_builder,
    )


def test_order_plan_is_stable_and_one_to_one_with_evidence():
    observed = {}
    engine = _engine_with_observer(observed)
    kwargs = {
        "portfolio_id": 1,
        "strategy_snapshot_id": "snapshot-plan-seam",
        "trade_date": date(2025, 1, 6),
        "run_type": "backtest",
        "dry_run": True,
    }

    first = engine.evaluate(None, **kwargs)
    second = engine.evaluate(None, **kwargs)

    assert len(first.order_plans) == len(first.evidence) == 1
    assert first.order_plans[0].evidence_id == second.order_plans[0].evidence_id
    assert first.order_plans[0].order_plan_id == second.order_plans[0].order_plan_id
    assert first.order_plans[0].target_quantity == 100.0
    assert first.order_plans[0].direction == "BUY"


def test_state_context_overrides_live_allocator_inputs_and_changes_identity():
    observed = {}
    engine = _engine_with_observer(observed)
    context = DecisionStateContext(
        current_by_symbol={7: {"qty": 0.0, "pct": 0.0}},
        available_cash=500.0,
        total_capital=1_000.0,
        price_data_by_symbol={7: {"open_price": 12.5, "close_price": 12.0}},
    )

    result = engine.evaluate(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-plan-seam",
        trade_date=date(2025, 1, 6),
        run_type="backtest",
        dry_run=True,
        state_context=context,
    )

    assert observed["override"]["available_cash"] == 500.0
    assert observed["override"]["total_capital"] == 1_000.0
    assert observed["price_hints"] == {7: 12.5}
    assert result.decision_run_id != engine.evaluate(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-plan-seam",
        trade_date=date(2025, 1, 6),
        run_type="backtest",
        dry_run=True,
    ).decision_run_id


def test_cost_and_slippage_changes_do_not_recompute_decision_identity():
    engine = _engine_with_observer({})
    base = {
        "current_by_symbol": {7: {"qty": 0.0, "pct": 0.0}},
        "available_cash": 500.0,
        "total_capital": 1_000.0,
        "price_data_by_symbol": {7: {"open_price": 12.5, "close_price": 12.0}},
    }
    first = engine.evaluate(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-plan-seam",
        trade_date=date(2025, 1, 6),
        run_type="backtest",
        dry_run=True,
        state_context=DecisionStateContext(**base, cost_config={"slippage_buy_bps": 5}),
    )
    second = engine.evaluate(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-plan-seam",
        trade_date=date(2025, 1, 6),
        run_type="backtest",
        dry_run=True,
        state_context=DecisionStateContext(**base, cost_config={"slippage_buy_bps": 25}),
    )
    assert first.decision_run_id == second.decision_run_id


def test_execution_price_hints_do_not_change_decision_identity():
    engine = _engine_with_observer({})
    base = {
        "current_by_symbol": {7: {"qty": 0.0, "pct": 0.0}},
        "available_cash": 500.0,
        "total_capital": 1_000.0,
    }
    first = engine.evaluate(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-plan-seam",
        trade_date=date(2025, 1, 6),
        run_type="backtest",
        dry_run=True,
        state_context=DecisionStateContext(
            **base,
            price_data_by_symbol={7: {"open_price": 12.5, "close_price": 12.0}},
        ),
    )
    second = engine.evaluate(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-plan-seam",
        trade_date=date(2025, 1, 6),
        run_type="backtest",
        dry_run=True,
        state_context=DecisionStateContext(
            **base,
            price_data_by_symbol={7: {"open_price": 13.5, "close_price": 13.0}},
        ),
    )

    assert first.decision_run_id == second.decision_run_id


def test_initial_capital_and_cash_are_execution_inputs_not_decision_identity():
    engine = _engine_with_observer({})
    common = {
        "current_by_symbol": {7: {"qty": 0.0, "pct": 0.0}},
        "price_data_by_symbol": {7: {"open_price": 12.5, "close_price": 12.0}},
    }
    first = engine.evaluate(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-plan-seam",
        trade_date=date(2025, 1, 6),
        run_type="backtest",
        dry_run=True,
        state_context=DecisionStateContext(
            **common, available_cash=500.0, total_capital=1_000.0
        ),
    )
    second = engine.evaluate(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-plan-seam",
        trade_date=date(2025, 1, 6),
        run_type="backtest",
        dry_run=True,
        state_context=DecisionStateContext(
            **common, available_cash=5_000.0, total_capital=10_000.0
        ),
    )

    assert first.decision_run_id == second.decision_run_id
