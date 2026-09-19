import json
from datetime import date, datetime
from types import SimpleNamespace

from app.models.daily_bar import DailyBar
from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import execute_member_source


def test_snapshot_auto_simulation_uses_decision_order_plans(monkeypatch):
    observed = {}

    def fake_evaluate(db, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            decision_run_id="decision-auto-1",
            order_plans=[SimpleNamespace(
                symbol_id=7,
                action="BUY",
                target_quantity=100.0,
                intended_price=10.0,
                reason_code="SIGNAL_OPEN",
                evidence_id="evidence-auto-1",
                order_plan_id="plan-auto-1",
                signal_date=date(2025, 1, 7),
                execution_date=date(2025, 1, 8),
            )],
        )

    from app.services import decision_engine
    monkeypatch.setattr(decision_engine.default_engine, "evaluate", fake_evaluate)

    result = execute_member_source(
        None,
        portfolio_id=1,
        strategy_snapshot_id="snapshot-auto-1",
        trade_date=date(2025, 1, 7),
        dry_run=True,
    )

    assert observed["run_type"] == "auto_simulation"
    assert observed["strategy_snapshot_id"] == "snapshot-auto-1"
    assert observed["trade_date"] == date(2025, 1, 7)
    assert observed["match_mode"] == "NEXT_OPEN"
    assert result["decision_run_id"] == "decision-auto-1"
    assert result["buy_decisions"][0]["evidence_id"] == "evidence-auto-1"
    assert result["buy_decisions"][0]["order_plan_id"] == "plan-auto-1"


def test_snapshot_auto_simulation_injects_locked_state_and_pit_prices(
    db_session, monkeypatch,
):
    """The auto entry must give DecisionEngine the same state inputs as replay."""
    portfolio = Portfolio(
        name="auto-decision-state-context",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=20_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        currency="CNY",
    )
    symbol = Symbol(
        symbol="600733",
        name="Auto State Context",
        asset_type="stock",
        market="SH",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    snapshot = StrategyExecutionSnapshot(
        id="snapshot-auto-state-context",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        decision_clock_json="{}",
        cost_config_json='{"slippage_buy_bps": 5, "min_lot_size": 100}',
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id}]),
        snapshot_type="save_and_apply",
        snapshot_hash="snapshot-auto-state-context-hash",
        effective_from=datetime(2025, 1, 1),
    )
    db_session.add_all([
        snapshot,
        DailyBar(
            symbol_id=symbol.id,
            trade_date=date(2026, 1, 6),
            open=10.0,
            high=10.2,
            low=9.8,
            close=10.0,
            volume=1_000_000,
        ),
        DailyBar(
            symbol_id=symbol.id,
            trade_date=date(2026, 1, 7),
            open=10.5,
            high=10.7,
            low=10.3,
            close=10.5,
            volume=1_000_000,
        ),
    ])
    db_session.commit()
    observed = {}

    def fake_evaluate(_db, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(decision_run_id="auto-state-context-run", order_plans=[])

    from app.services import decision_engine
    monkeypatch.setattr(decision_engine.default_engine, "evaluate", fake_evaluate)

    execute_member_source(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id,
        trade_date=date(2026, 1, 6),
        dry_run=True,
    )

    context = observed["state_context"]
    assert context.available_cash == portfolio.total_capital
    assert context.total_capital == portfolio.total_capital
    assert context.current_by_symbol == {}
    assert context.price_data_by_symbol[symbol.id]["close_price"] == 10.0
    assert context.price_data_by_symbol[symbol.id]["open_price"] == 10.5
    assert context.cost_config["slippage_buy_bps"] == 5
