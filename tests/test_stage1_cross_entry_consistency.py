"""Stage-1 cross-entry decision contract tests.

The entry points may use different run identities, but a fixed snapshot,
trade date and injected portfolio state must produce the same per-security
order intent.  Execution differences belong to the matching layer only.
"""

from datetime import date, datetime
import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import select

import pytest

from app.models.backtest import BacktestExecutionFill, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.sim_account import SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import execute_member_source
from app.services.backtest import run_backtest
from app.services import decision_engine
from app.services.decision_engine import DecisionStateContext, default_engine
from app.services.sim_accounts import cash_balance


def _canonical_plan(plan):
    return {
        "symbol_id": int(plan.symbol_id),
        "action": str(plan.action),
        "target_quantity": float(plan.target_quantity),
        "direction": plan.direction,
        "reason_code": plan.reason_code,
        "signal_date": plan.signal_date.isoformat(),
        "execution_date": plan.execution_date.isoformat(),
    }


def _digest(plans):
    payload = [_canonical_plan(p) for p in sorted(plans, key=lambda p: int(p.symbol_id))]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def test_dry_run_backtest_and_auto_simulation_have_same_decision_hash(monkeypatch):
    plans = [
        SimpleNamespace(
            symbol_id=7,
            action="BUY",
            target_quantity=200.0,
            direction="BUY",
            reason_code="SIGNAL_OPEN",
            signal_date=date(2025, 1, 7),
            execution_date=date(2025, 1, 8),
        ),
        SimpleNamespace(
            symbol_id=9,
            action="SELL",
            target_quantity=100.0,
            direction="SELL",
            reason_code="RISK_EXIT",
            signal_date=date(2025, 1, 7),
            execution_date=date(2025, 1, 8),
        ),
    ]
    calls = []

    def fake_evaluate(db, **kwargs):
        calls.append(kwargs)
        # Simulate separate run IDs while retaining the same immutable plan.
        return SimpleNamespace(
            decision_run_id=f"run-{kwargs['run_type']}",
            order_plans=plans,
        )

    monkeypatch.setattr(decision_engine.default_engine, "evaluate", fake_evaluate)
    state = SimpleNamespace(available_cash=100000.0, current_by_symbol={}, price_data_by_symbol={})
    results = [
        decision_engine.default_engine.evaluate(
            None,
            portfolio_id=1,
            strategy_snapshot_id="snapshot-fixed",
            trade_date=date(2025, 1, 7),
            run_type=run_type,
            dry_run=True,
            state_context=state,
            match_mode="NEXT_OPEN",
        )
        for run_type in ("research_preflight", "backtest", "auto_simulation")
    ]

    assert [call["strategy_snapshot_id"] for call in calls] == ["snapshot-fixed"] * 3
    assert [_digest(result.order_plans) for result in results] == [
        _digest(results[0].order_plans)
    ] * 3
    assert [result.decision_run_id for result in results] == [
        "run-research_preflight",
        "run-backtest",
        "run-auto_simulation",
    ]


_TRADE_DATE = date(2026, 1, 6)
_EXECUTION_DATE = date(2026, 1, 7)


def _plan_payload(plan):
    """Keep run/evidence identities out of the business-plan comparison.

    The three public entries deliberately have different run identities.  The
    contract under test is the immutable per-symbol intent that arrives at the
    matcher, rather than its entry-specific persistence key.
    """
    return {
        "symbol_id": int(plan["symbol_id"]),
        "action": str(plan["action"]),
        "target_quantity": float(plan["target_quantity"]),
        "direction": plan["direction"],
        "intended_price": (
            None if plan["intended_price"] is None else float(plan["intended_price"])
        ),
        "reason_code": plan["reason_code"],
        "signal_date": str(plan["signal_date"]),
        "execution_date": str(plan["execution_date"]),
        "rejection_trace": plan["rejection_trace"],
    }


def _per_symbol_plan_hashes(plans):
    hashes = {}
    for plan in plans:
        payload = _plan_payload(plan)
        hashes[payload["symbol_id"]] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    return hashes


def _result_plans(result):
    return [
        {
            "symbol_id": plan.symbol_id,
            "action": plan.action,
            "target_quantity": plan.target_quantity,
            "direction": plan.direction,
            "intended_price": plan.intended_price,
            "reason_code": plan.reason_code,
            "signal_date": plan.signal_date.isoformat(),
            "execution_date": plan.execution_date.isoformat(),
            "rejection_trace": plan.rejection_trace,
        }
        for plan in result.order_plans
    ]


def _persisted_plans(evidence_rows):
    """Read the durable plan envelope rather than reconstructing it in test code."""
    plans = []
    for evidence in evidence_rows:
        envelope = json.loads(evidence.versions_json or "{}")
        required = {
            "order_plan_action",
            "order_plan_target_quantity",
            "order_plan_direction",
            "order_plan_intended_price",
            "order_plan_reason_code",
            "order_plan_signal_date",
            "order_plan_execution_date",
            "order_plan_rejection_trace",
        }
        assert required <= set(envelope), (
            f"evidence {evidence.id} lost immutable order-plan envelope: "
            f"keys={sorted(envelope)}"
        )
        plans.append({
            "symbol_id": evidence.symbol_id,
            "action": envelope["order_plan_action"],
            "target_quantity": envelope["order_plan_target_quantity"],
            "direction": envelope["order_plan_direction"],
            "intended_price": envelope["order_plan_intended_price"],
            "reason_code": envelope["order_plan_reason_code"],
            "signal_date": envelope["order_plan_signal_date"],
            "execution_date": envelope["order_plan_execution_date"],
            "rejection_trace": envelope["order_plan_rejection_trace"],
        })
    return plans


def _seed_real_tri_entry_case(db_session):
    """Create a deterministic snapshot-backed, two-symbol decision day."""
    portfolio = Portfolio(
        name="stage1-real-tri-entry",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        default_single_position_pct=0.10,
        currency="CNY",
        auto_trade_enabled=1,
    )
    buy_symbol = Symbol(
        symbol="600731",
        name="Tri Entry Buy",
        market="SH",
        asset_type="stock",
        industry="tri-entry",
        listed_at=date(2020, 1, 1),
    )
    hold_symbol = Symbol(
        symbol="600732",
        name="Tri Entry Hold",
        market="SH",
        asset_type="stock",
        industry="tri-entry",
        listed_at=date(2020, 1, 1),
    )
    db_session.add_all([portfolio, buy_symbol, hold_symbol])
    db_session.flush()

    # Both days use the same open so a one-decision-day backtest and the
    # historical auto-simulation fixture share an identical price assumption.
    for symbol, quality_score in ((buy_symbol, 0.90), (hold_symbol, 0.50)):
        for trade_date in (_TRADE_DATE, _EXECUTION_DATE):
            db_session.add(DailyBar(
                symbol_id=symbol.id,
                trade_date=trade_date,
                open=10.0,
                high=10.2,
                low=9.8,
                close=10.0,
                volume=1_000_000,
                amount=10_000_000,
            ))
        db_session.add(Score(
            symbol_id=symbol.id,
            trade_date=_TRADE_DATE,
            quality_score=quality_score,
            quality_grade="A",
            timing_score=0.8,
            stage="growth",
            action="open" if quality_score >= 0.7 else "hold",
            priority_score=quality_score,
            weight_mode="manual",
            factor_model_run_id="stage1-real-tri-entry-model",
            calc_batch_id=f"stage1-real-tri-entry-{symbol.id}",
            published_at=datetime(2026, 1, 1, 0, 0),
        ))

    snapshot = StrategyExecutionSnapshot(
        id="stage1-real-tri-entry-snapshot",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        factor_model_run_id="stage1-real-tri-entry-model",
        decision_clock_json="{}",
        cost_config_json=json.dumps({
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "stamp_tax_rate": 0.001,
            "transfer_fee_rate": 0.00001,
            "slippage_buy_bps": 5,
            "slippage_sell_bps": 5,
            "min_lot_size": 100,
        }),
        member_snapshot_json=json.dumps([
            {"symbol_id": buy_symbol.id, "member_id": 1},
            {"symbol_id": hold_symbol.id, "member_id": 2},
        ]),
        snapshot_type="save_and_apply",
        snapshot_hash="stage1-real-tri-entry-hash",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({
            "run_mode": "research",
            "pit_mode": "best_effort",
            "signal_policy": {"buy_threshold": 0.7, "sell_threshold": 0.35},
        }),
        effective_from=datetime(2026, 1, 1),
    )
    db_session.add(snapshot)
    db_session.commit()
    return portfolio, (buy_symbol, hold_symbol), snapshot


def test_real_sqlite_tri_entry_hashes_match_before_matching(db_session):
    """§17.4: real public entries produce the same immutable order plans.

    The backtest has exactly one decision day.  Its declared T+1 execution is
    intentionally outside the range, proving that the assertion compares
    decision plans before matching rather than comparing incidental fills.
    Auto simulation then consumes the same plan on the isolated next-day bar;
    its mutable fill fields are deliberately not included in the hash.
    """
    portfolio, symbols, snapshot = _seed_real_tri_entry_case(db_session)
    price_data = {
        symbol.id: {
            "open_price": 10.0,
            "close_price": 10.0,
            "high_price": 10.2,
            "low_price": 9.8,
            "volume": 1_000_000,
            "prev_close_price": 10.0,
            "is_suspended_today": False,
        }
        for symbol in symbols
    }
    common_state = DecisionStateContext(
        current_by_symbol={},
        available_cash=portfolio.total_capital,
        total_capital=portfolio.total_capital,
        price_data_by_symbol=price_data,
        cost_config=json.loads(snapshot.cost_config_json),
    )

    dry_run = default_engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id,
        trade_date=_TRADE_DATE,
        run_type="research_preflight",
        dry_run=True,
        state_context=common_state,
        match_mode="NEXT_OPEN",
    )
    backtest = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id for symbol in symbols],
        start_date=_TRADE_DATE,
        end_date=_TRADE_DATE,
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.05}},
        initial_capital=portfolio.total_capital,
        cost_config=json.loads(snapshot.cost_config_json),
        strategy_snapshot_id=snapshot.id,
    )
    backtest_evidence = db_session.execute(
        select(DecisionEvidence)
        .join(DecisionRun, DecisionRun.id == DecisionEvidence.decision_run_id)
        .where(
            DecisionRun.id.in_(json.loads(backtest.decision_run_ids_json or "[]")),
            DecisionEvidence.trade_date == _TRADE_DATE,
        )
        .order_by(DecisionEvidence.symbol_id)
    ).scalars().all()

    auto_result = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id,
        trade_date=_TRADE_DATE,
        dry_run=False,
    )
    auto_evidence = db_session.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.decision_run_id == auto_result["decision_run_id"],
            DecisionEvidence.trade_date == _TRADE_DATE,
        ).order_by(DecisionEvidence.symbol_id)
    ).scalars().all()

    dry_hashes = _per_symbol_plan_hashes(_result_plans(dry_run))
    backtest_hashes = _per_symbol_plan_hashes(_persisted_plans(backtest_evidence))
    auto_hashes = _per_symbol_plan_hashes(_persisted_plans(auto_evidence))

    assert dry_hashes == backtest_hashes == auto_hashes, json.dumps(
        {
            "dry_run": _result_plans(dry_run),
            "backtest": _persisted_plans(backtest_evidence),
            "auto_simulation": _persisted_plans(auto_evidence),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert set(dry_hashes) == {symbol.id for symbol in symbols}
    assert any(plan.action == "BUY" and plan.target_quantity > 0 for plan in dry_run.order_plans)

    # The single-day backtest keeps its T+1 plan pending; auto uses the
    # available historical execution bar. These are matching outcomes only.
    assert db_session.execute(
        select(BacktestTrade).where(BacktestTrade.run_id == backtest.id)
    ).scalars().all() == []
    assert auto_result["executed_orders"]
    auto_buy_evidence = next(row for row in auto_evidence if row.action == "BUY")
    immutable_plan = json.loads(auto_buy_evidence.versions_json)
    assert auto_buy_evidence.executed_price is not None
    assert auto_buy_evidence.executed_price != immutable_plan["order_plan_intended_price"]


def test_real_tri_entry_reconciles_execution_events_and_account_cash(db_session):
    """§17.4: the same plan fills identically in backtest and auto simulation.

    This is intentionally an end-to-end reconciliation rather than a matcher
    unit test.  All three public entries read the same snapshot, scores and
    DailyBars.  The backtest persists its immutable fill event; automatic
    simulation persists the corresponding SimOrder/SimTrade/account ledger.
    Run/evidence identifiers legitimately differ by entry, so the comparison
    is made on immutable business-plan fields and execution facts.
    """
    portfolio, symbols, snapshot = _seed_real_tri_entry_case(db_session)
    price_data = {
        symbol.id: {
            "open_price": 10.0,
            "close_price": 10.0,
            "high_price": 10.2,
            "low_price": 9.8,
            "volume": 1_000_000,
            "prev_close_price": 10.0,
            "is_suspended_today": False,
        }
        for symbol in symbols
    }
    common_state = DecisionStateContext(
        current_by_symbol={},
        available_cash=portfolio.total_capital,
        total_capital=portfolio.total_capital,
        price_data_by_symbol=price_data,
        cost_config=json.loads(snapshot.cost_config_json),
    )

    dry_run = default_engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id,
        trade_date=_TRADE_DATE,
        run_type="research_preflight",
        dry_run=True,
        state_context=common_state,
        match_mode="NEXT_OPEN",
    )
    backtest = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id for symbol in symbols],
        start_date=_TRADE_DATE,
        end_date=_EXECUTION_DATE,
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.05}},
        initial_capital=portfolio.total_capital,
        cost_config=json.loads(snapshot.cost_config_json),
        strategy_snapshot_id=snapshot.id,
    )
    backtest_evidence = db_session.execute(
        select(DecisionEvidence)
        .join(DecisionRun, DecisionRun.id == DecisionEvidence.decision_run_id)
        .where(
            DecisionRun.id.in_(json.loads(backtest.decision_run_ids_json or "[]")),
            DecisionEvidence.trade_date == _TRADE_DATE,
        )
        .order_by(DecisionEvidence.symbol_id)
    ).scalars().all()
    auto_result = execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id,
        trade_date=_TRADE_DATE,
        dry_run=False,
    )
    auto_evidence = db_session.execute(
        select(DecisionEvidence)
        .where(
            DecisionEvidence.decision_run_id == auto_result["decision_run_id"],
            DecisionEvidence.trade_date == _TRADE_DATE,
        )
        .order_by(DecisionEvidence.symbol_id)
    ).scalars().all()

    dry_hashes = _per_symbol_plan_hashes(_result_plans(dry_run))
    backtest_hashes = _per_symbol_plan_hashes(_persisted_plans(backtest_evidence))
    auto_hashes = _per_symbol_plan_hashes(_persisted_plans(auto_evidence))
    assert dry_hashes == backtest_hashes == auto_hashes

    buy_symbol = next(symbol for symbol in symbols if symbol.symbol == "600731")
    backtest_fill = db_session.execute(
        select(BacktestExecutionFill).where(
            BacktestExecutionFill.run_id == backtest.id,
            BacktestExecutionFill.symbol_id == buy_symbol.id,
            BacktestExecutionFill.side == "BUY",
        )
    ).scalar_one()
    auto_buy_evidence = next(
        row for row in auto_evidence
        if row.symbol_id == buy_symbol.id and row.action == "BUY"
    )
    auto_order = db_session.execute(
        select(SimOrder).where(
            SimOrder.portfolio_id == portfolio.id,
            SimOrder.decision_evidence_id == auto_buy_evidence.id,
        )
    ).scalar_one()
    auto_trade = db_session.execute(
        select(SimTrade).where(SimTrade.order_id == auto_order.id)
    ).scalar_one()

    backtest_buy_evidence = next(
        row for row in backtest_evidence
        if row.symbol_id == buy_symbol.id and row.action == "BUY"
    )
    backtest_costs = json.loads(backtest_buy_evidence.versions_json or "{}")
    auto_costs = json.loads(auto_buy_evidence.versions_json or "{}")
    for cost_key in (
        "order_plan_commission",
        "order_plan_stamp_tax",
        "order_plan_transfer_fee",
        "order_plan_total_cost",
    ):
        assert backtest_costs[cost_key] == pytest.approx(auto_costs[cost_key])
    assert backtest_costs["order_plan_total_cost"] == pytest.approx(backtest_fill.cost)

    assert backtest_fill.execution_date == _EXECUTION_DATE
    assert auto_trade.quantity == pytest.approx(backtest_fill.quantity)
    assert auto_trade.price == pytest.approx(backtest_fill.executed_price)
    assert auto_trade.fee == pytest.approx(round(backtest_fill.cost, 2))
    assert auto_order.filled_quantity == pytest.approx(backtest_fill.quantity)
    assert auto_order.filled_price == pytest.approx(backtest_fill.executed_price)
    assert auto_order.fee == pytest.approx(round(backtest_fill.cost, 2))

    backtest_curve = {
        point["date"]: point
        for point in json.loads(backtest.equity_curve_json or "[]")
    }
    expected_cash = backtest_curve[_EXECUTION_DATE.isoformat()]["cash"]
    assert cash_balance(db_session, portfolio.id) == pytest.approx(expected_cash)
