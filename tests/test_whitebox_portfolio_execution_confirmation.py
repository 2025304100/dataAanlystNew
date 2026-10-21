"""Actual auto entry and BUY ledger boundaries honor user acceptance locks."""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionEvidence, DecisionOrderPlanRecord, DecisionRun
from app.models.portfolio import Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import execute_member_source
from app.services.decision_engine import DecisionEngine, PerSymbolEvidence, auto_simulation_governance_reasons, default_engine
from tests.test_stage3_factor_binding_contract import _ready_model
from tests.test_whitebox_portfolio_confirmation import binding, _legacy_state, _snapshot

pytestmark = pytest.mark.whitebox
TRADE_DATE = date(2026, 9, 2)


@pytest.mark.parametrize("field,state", [
    ("status_model", "MODEL_INACTIVE"),
    ("status_reconciliation", "RECONCILIATION_BLOCKED"),
    ("status_data", "DATA_INCOMPLETE_PAUSED"),
])
def test_shared_engine_refuses_unready_persisted_auto_without_writes(binding, field, state):
    db, portfolio, _, _, snapshot = binding
    portfolio.status_model = "READY"
    setattr(portfolio, field, state)
    _legacy_state(db, portfolio, "READY")
    db.commit()
    with pytest.raises(HTTPException) as error:
        default_engine.evaluate(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, run_type="auto_simulation", dry_run=False)
    assert error.value.status_code == 409
    assert db.scalar(select(func.count()).select_from(DecisionRun)) == 0
    assert getattr(portfolio, field) == state


@pytest.mark.parametrize("run_type,dry_run", [("auto_simulation", True), ("backtest", True), ("backtest", False)])
def test_model_lock_preserves_diagnostic_and_backtest_access(binding, run_type, dry_run):
    db, portfolio, _, _, snapshot = binding
    _legacy_state(db, portfolio, "ADMIN_PAUSED")
    db.commit()
    result = default_engine.evaluate(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, run_type=run_type, dry_run=dry_run)
    assert result.persisted is (not dry_run)
    assert portfolio.status_model == "MODEL_INACTIVE"


@pytest.mark.parametrize("runtime_state", ["READY", "RUNNING_AUTO_SIMULATION"])
def test_confirmed_binding_accepts_ready_or_normal_preflight_running_state(binding, runtime_state):
    db, portfolio, _, _, snapshot = binding
    portfolio.status_model = "READY"
    _legacy_state(db, portfolio, runtime_state)
    db.commit()
    result = default_engine.evaluate(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, run_type="auto_simulation", dry_run=False)
    assert result.persisted is True


def test_engine_rejects_global_model_switched_even_without_portfolio_dimension_change(binding):
    from app.models.factor_runtime import FactorRuntimeState
    db, portfolio, _, _, snapshot = binding
    portfolio.status_model = "READY"
    _legacy_state(db, portfolio, "READY")
    _ready_model(db, set_id="fs-execution-next", model_id="model-execution-next")
    db.get(FactorRuntimeState, 1).active_model_run_id = "model-execution-next"
    db.commit()
    with pytest.raises(HTTPException) as error:
        default_engine.evaluate(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, run_type="auto_simulation", dry_run=False)
    assert error.value.status_code == 409
    assert db.scalar(select(func.count()).select_from(DecisionRun)) == 0
    assert portfolio.status_model == "READY"


def test_final_current_read_rechecks_cached_runtime_before_buy(binding, monkeypatch):
    from app.models.factor_runtime import FactorRuntimeState
    from sqlalchemy.dialects import mysql
    db, portfolio, _, _, snapshot = binding
    portfolio.status_model = "READY"
    _legacy_state(db, portfolio, "READY")
    _ready_model(db, set_id="fs-cache-next", model_id="model-cache-next")
    db.commit()
    cached = db.get(FactorRuntimeState, 1)
    assert cached.active_model_run_id == snapshot.factor_model_run_id
    db.execute(text("UPDATE factor_runtime_state SET active_model_run_id='model-cache-next' WHERE id=1"))
    assert cached.active_model_run_id == snapshot.factor_model_run_id
    # Inspect the actual statements used by this execution boundary, compiling
    # as MySQL: SQLite exercises row refresh but cannot exercise RR row locks.
    statements = []
    execute = db.execute
    def capture(statement, *args, **kwargs):
        statements.append(str(statement.compile(dialect=mysql.dialect())))
        return execute(statement, *args, **kwargs)
    monkeypatch.setattr(db, "execute", capture)
    blockers = auto_simulation_governance_reasons(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, current_read=True)
    assert any("当前活动模型" in reason["message"] for reason in blockers)
    assert cached.active_model_run_id == "model-cache-next"
    for table in ("portfolios", "factor_runtime_state", "strategy_execution_snapshots", "portfolio_factor_usages"):
        assert any(f"FROM {table}" in sql and "FOR UPDATE" in sql for sql in statements), table


@pytest.mark.parametrize("invalid", [None, "different_model", "unapplied", "missing_usage"])
def test_historical_applied_replay_requires_unchanged_current_binding(binding, invalid):
    db, portfolio, _, usage, old = binding
    portfolio.status_model = "READY"
    _legacy_state(db, portfolio, "READY")
    usage.status = "deprecated"
    _snapshot(db, portfolio, model_id=old.factor_model_run_id, set_id=old.factor_set_id, number=2)
    if invalid == "different_model":
        old.factor_model_run_id = usage.factor_model_run_id = "obsolete-model"
    elif invalid == "unapplied":
        old.snapshot_type = "preflight"
    elif invalid == "missing_usage":
        old.portfolio_factor_usage_id = None
    db.commit()
    if invalid is None:
        result = default_engine.evaluate(db, portfolio_id=portfolio.id, strategy_snapshot_id=old.id, trade_date=TRADE_DATE, run_type="auto_simulation", dry_run=False)
        assert result.persisted is True
    else:
        with pytest.raises(HTTPException) as error:
            default_engine.evaluate(db, portfolio_id=portfolio.id, strategy_snapshot_id=old.id, trade_date=TRADE_DATE, run_type="auto_simulation", dry_run=False)
        assert error.value.status_code == 409
        assert db.scalar(select(func.count()).select_from(DecisionRun)) == 0


def test_guarded_engine_only_rejects_buy_evidence_and_retains_sell(binding):
    db, portfolio, _, _, snapshot = binding
    symbols = [Symbol(symbol="600991", name="Entry", market="SH", asset_type="stock"), Symbol(symbol="600992", name="Risk exit", market="SH", asset_type="stock")]
    db.add_all(symbols)
    db.flush()
    engine = DecisionEngine(evidence_builder=lambda **kwargs: [
        PerSymbolEvidence(symbol_id=symbols[0].id, action="BUY", target_quantity=100, target_qty_delta=100, intended_price=10),
        PerSymbolEvidence(symbol_id=symbols[1].id, action="SELL", action_subtype="SELL_STOP_LOSS", target_quantity=0, target_qty_delta=-100, intended_price=10),
    ])
    _legacy_state(db, portfolio, "READY")
    db.commit()
    result = engine.evaluate(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, run_type="auto_simulation", dry_run=False, governance_exit_only=True)
    assert [(plan.action, plan.target_quantity) for plan in result.order_plans] == [("REJECTED", 0), ("SELL", 100)]
    assert result.evidence[0].rejection_reason == "PORTFOLIO_EXECUTION_BLOCKED"
    assert result.evidence[1].action_subtype == "SELL_STOP_LOSS"
    assert portfolio.status_model == "MODEL_INACTIVE"


def test_same_input_governance_change_has_stable_distinct_run_identity_with_real_foreign_keys(binding):
    db, portfolio, _, _, snapshot = binding
    symbol = Symbol(symbol="600990", name="Stable audited identity", market="SH", asset_type="stock")
    db.add(symbol)
    portfolio.status_model = "READY"
    _legacy_state(db, portfolio, "READY")
    db.commit()
    db.execute(text("PRAGMA foreign_keys=ON"))
    assert db.scalar(text("PRAGMA foreign_keys")) == 1
    engine = DecisionEngine(evidence_builder=lambda **kwargs: [
        PerSymbolEvidence(symbol_id=symbol.id, action="BUY", target_quantity=100, target_qty_delta=100, intended_price=10),
    ])
    arguments = dict(portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, run_type="auto_simulation", dry_run=False, governance_exit_only=True)
    initial = engine.evaluate(db, **arguments)
    db.commit()
    portfolio.status_model = "MODEL_INACTIVE"
    db.commit()
    locked = engine.evaluate(db, **arguments)
    db.commit()
    repeated = engine.evaluate(db, **arguments)
    db.commit()
    assert initial.order_plans[0].action == "BUY"
    assert locked.order_plans[0].action == "REJECTED"
    assert locked.decision_run_id != initial.decision_run_id
    assert repeated.decision_run_id == locked.decision_run_id
    assert db.scalar(select(func.count()).select_from(DecisionRun)) == 2
    assert db.scalar(select(func.count()).select_from(DecisionEvidence)) == 2
    assert db.scalar(select(func.count()).select_from(DecisionOrderPlanRecord)) == 2
    portfolio.status_model = "READY"
    db.commit()
    unlocked = engine.evaluate(db, **arguments)
    db.commit()
    assert unlocked.decision_run_id == initial.decision_run_id
    assert unlocked.order_plans[0].evidence_id == initial.order_plans[0].evidence_id
    assert db.scalar(select(func.count()).select_from(DecisionOrderPlanRecord)) == 2


@pytest.mark.parametrize("blocker", ["model_switch", "model_unconfirmed"])
def test_actual_ledger_buy_boundary_blocks_entry_but_fills_sell(binding, monkeypatch, blocker):
    from app.models.factor_runtime import FactorRuntimeState
    db, portfolio, _, _, snapshot = binding
    portfolio.account_type = "simulated"
    portfolio.status_model = "READY" if blocker == "model_switch" else "MODEL_INACTIVE"
    _legacy_state(db, portfolio, "READY")
    buy_symbol = Symbol(symbol="600993", name="Entry", market="SH", asset_type="stock")
    sell_symbol = Symbol(symbol="600994", name="Exit", market="SH", asset_type="stock")
    db.add_all([buy_symbol, sell_symbol])
    db.flush()
    snapshot.member_snapshot_json = json.dumps([{"symbol_id": buy_symbol.id}, {"symbol_id": sell_symbol.id}])
    db.add(Position(portfolio_id=portfolio.id, symbol_id=sell_symbol.id, quantity=100, avg_cost=10, latest_price=10, asset_type="stock", opened_at=date(2026, 8, 1)))
    for symbol in (buy_symbol, sell_symbol):
        for day in (date(2026, 9, 1), TRADE_DATE, date(2026, 9, 3)):
            db.add(DailyBar(symbol_id=symbol.id, trade_date=day, open=10, high=10.2, low=9.8, close=10, volume=1_000_000))
    if blocker == "model_switch":
        _ready_model(db, set_id="fs-ledger-next", model_id="model-ledger-next")
        db.get(FactorRuntimeState, 1).active_model_run_id = "model-ledger-next"
    run = DecisionRun(
        id="execution-confirmation-run", portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=TRADE_DATE, run_type="auto_simulation", status="SUCCEEDED",
        versions_json=json.dumps({"price_reference_policy_version": "signal-close-v1"}),
        decision_at=datetime(2026, 9, 2, 7, 5), data_cutoff_at=datetime(2026, 9, 2, 7), execution_at=datetime(2026, 9, 3, 1, 30),
    )
    db.add(run)
    db.flush()
    plans = []
    for action, symbol in (("BUY", buy_symbol), ("SELL", sell_symbol)):
        evidence_id = f"execution-evidence-{action}"
        db.add(DecisionEvidence(
            id=evidence_id, portfolio_id=portfolio.id, symbol_id=symbol.id, strategy_snapshot_id=snapshot.id,
            decision_run_id=run.id, trade_date=TRADE_DATE, action=action, target_quantity=100,
            target_qty_delta=100 if action == "BUY" else -100,
            decision_at=run.decision_at, data_cutoff_at=run.data_cutoff_at, execution_at=run.execution_at,
            content_hash=f"execution-hash-{action}",
        ))
        plans.append(SimpleNamespace(symbol_id=symbol.id, action=action, target_quantity=100, intended_price=10, reason_code="TEST", evidence_id=evidence_id, order_plan_id=f"execution-plan-{action}", signal_date=TRADE_DATE, execution_date=date(2026, 9, 3)))
    db.commit()
    # Even an older persisted plan or a replaced evaluator must pass the real
    # final entry boundary before it reaches matching and the cash ledger.
    monkeypatch.setattr(default_engine, "evaluate", lambda *args, **kwargs: SimpleNamespace(decision_run_id=run.id, order_plans=plans))
    result = execute_member_source(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, dry_run=False)
    orders = db.execute(select(SimOrder).where(SimOrder.portfolio_id == portfolio.id)).scalars().all()
    buys = [order for order in orders if order.side == "buy"]
    sells = [order for order in orders if order.side == "sell"]
    assert len(buys) == 1 and buys[0].status == "rejected" and buys[0].filled_quantity == 0
    assert len(sells) == 1 and sells[0].filled_quantity == 100
    assert any(item["symbol_id"] == buy_symbol.id for item in result["rejected_decisions"])
    assert (db.scalar(select(Position.quantity).where(Position.portfolio_id == portfolio.id, Position.symbol_id == sell_symbol.id)) or 0) == 0
    assert db.scalar(select(Position.id).where(Position.portfolio_id == portfolio.id, Position.symbol_id == buy_symbol.id)) is None
    replay = execute_member_source(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, dry_run=False)
    assert replay["errors"] == [] and replay["executed_orders"] == []
    assert db.scalar(select(func.count()).select_from(SimOrder)) == 2


@pytest.mark.parametrize("lock", ["unconfirmed", "global_model_switch", "drawdown"])
def test_real_partial_buy_retry_under_lock_keeps_original_identity_and_replay_does_not_fill_twice(binding, monkeypatch, lock):
    """A locked new decision cannot erase or duplicate a durable partial plan."""
    from app.models.factor_runtime import FactorRuntimeState
    db, portfolio, _, _, snapshot = binding
    portfolio.account_type = "simulated"
    portfolio.status_model = "READY"
    _legacy_state(db, portfolio, "READY")
    symbol = Symbol(symbol="600995", name="Partial entry", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    snapshot.member_snapshot_json = json.dumps([{"symbol_id": symbol.id, "execution_mode": "auto"}])
    snapshot.cost_config_json = json.dumps({"volume_limit_pct": 0.1, "slippage_buy_bps": 0})
    for day, volume in (
        (date(2026, 9, 1), 1000), (TRADE_DATE, 1000),
        (date(2026, 9, 3), 1000), (date(2026, 9, 4), 1000),
        (date(2026, 9, 7), 10000),
    ):
        db.add(DailyBar(symbol_id=symbol.id, trade_date=day, open=10, high=10.2, low=9.8, close=10, volume=volume,
            created_at=datetime.combine(day, datetime.min.time())))
    db.commit()
    engine = DecisionEngine(evidence_builder=lambda **kwargs: [
        PerSymbolEvidence(symbol_id=symbol.id, action="BUY", target_quantity=1000, target_qty_delta=1000, intended_price=10),
    ])
    monkeypatch.setattr(default_engine, "evaluate", engine.evaluate)

    first = execute_member_source(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=TRADE_DATE, dry_run=False)
    assert first["errors"] == []
    order = db.execute(select(SimOrder).where(SimOrder.portfolio_id == portfolio.id)).scalar_one()
    original = (order.id, order.client_order_key, order.decision_evidence_id)
    assert order.status == "partial" and order.quantity == 1000 and order.filled_quantity == 100
    original_plan = db.get(DecisionOrderPlanRecord, order.client_order_key)
    assert original_plan.signal_date == TRADE_DATE and original_plan.execution_date == date(2026, 9, 3)
    if lock == "unconfirmed":
        portfolio.status_model = "MODEL_INACTIVE"
    elif lock == "global_model_switch":
        _ready_model(db, set_id="fs-retry-next", model_id="model-retry-next")
        db.get(FactorRuntimeState, 1).active_model_run_id = "model-retry-next"
    else:
        from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
        snapshot.versions_json = json.dumps({"risk_policy": {"drawdown_circuit_pct": .10, "drawdown_action": "block_new_buys"}})
        db.add(PortfolioEquitySnapshot(portfolio_id=portfolio.id, snapshot_date=TRADE_DATE,
            total_equity=200000, created_at=datetime(2026, 9, 2, 6)))
    db.commit()

    # The first 100 shares already consumed Sep 3's volume. A retry must
    # reach Sep 4 before it may consume Sep 4's bar, even under an entry lock.
    second = execute_member_source(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 4), dry_run=False)
    assert second["errors"] == []
    db.refresh(order)
    assert (order.id, order.client_order_key, order.decision_evidence_id) == original
    assert order.status == "partial" and order.filled_quantity == 200
    # The fresh BUY really went through the shared engine and was rejected;
    # only the original persisted plan reached the matching/account adapter.
    later = db.execute(select(DecisionEvidence).where(DecisionEvidence.trade_date == date(2026, 9, 4))).scalar_one()
    assert later.action == "REJECTED" and later.id != original[2]
    assert later.rejection_reason == ("DRAWDOWN_CIRCUIT_TRIGGERED" if lock == "drawdown" else "PORTFOLIO_EXECUTION_BLOCKED")
    assert db.scalar(select(func.count()).select_from(SimOrder)) == 1
    assert db.scalar(select(func.count()).select_from(SimTrade)) == 2
    assert db.scalar(select(Position.quantity).where(Position.portfolio_id == portfolio.id, Position.symbol_id == symbol.id)) == 200
    cash_after_retry = db.scalar(select(func.sum(CashLedger.amount)).where(CashLedger.portfolio_id == portfolio.id))

    replay = execute_member_source(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 4), dry_run=False)
    assert replay["errors"] == [] and replay["executed_orders"] == []
    db.refresh(order)
    assert order.filled_quantity == 200
    assert db.scalar(select(func.count()).select_from(SimTrade)) == 2
    assert db.scalar(select(func.sum(CashLedger.amount)).where(CashLedger.portfolio_id == portfolio.id)) == cash_after_retry

    final = execute_member_source(db, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=date(2026, 9, 7), dry_run=False)
    assert final["errors"] == []
    db.refresh(order)
    assert (order.id, order.client_order_key, order.decision_evidence_id) == original
    assert order.status == "filled" and order.filled_quantity == 1000
    assert db.scalar(select(func.count()).select_from(SimOrder)) == 1
    assert db.scalar(select(func.sum(SimTrade.quantity))) == 1000
    assert db.scalar(select(Position.quantity).where(Position.portfolio_id == portfolio.id, Position.symbol_id == symbol.id)) == 1000
    trace = json.loads(db.get(DecisionEvidence, original[2]).rejections_trace_json)
    retries = [item for item in trace if item.get("reason") == "PARTIAL_FILL_RETRY"]
    assert [item["execution_date"] for item in retries] == ["2026-09-04", "2026-09-07"]
    assert all(item["execution_date"] <= item["trigger_trade_date"] for item in retries)
