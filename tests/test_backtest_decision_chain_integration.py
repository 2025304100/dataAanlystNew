from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.models.backtest import BacktestExecutionFill, BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.backtest import run_backtest
from app.services.decision_engine import default_engine


def test_backtest_persists_unified_decision_chain_and_links_trades(db_session):
    portfolio = Portfolio(
        name="decision-chain-integration",
        account_type="simulated",
        total_capital=100_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        currency="CNY",
    )
    symbol = Symbol(symbol="600000", name="Integration", market="SH", asset_type="stock")
    db_session.add_all([portfolio, symbol])
    db_session.flush()

    days = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
    for i, trade_date in enumerate(days):
        # Keep the fixture away from the 10% limit-up boundary so the
        # integration test exercises evidence linkage rather than rejection.
        close = 10.0 + i * 0.2
        db_session.add(DailyBar(
            symbol_id=symbol.id,
            trade_date=trade_date,
            open=close,
            high=close + 0.2,
            low=close - 0.2,
            close=close,
            volume=1_000_000,
            amount=close * 1_000_000,
        ))
        db_session.add(Score(
            symbol_id=symbol.id,
            trade_date=trade_date,
            quality_score=0.9 if i == 0 else 0.2,
            quality_grade="A",
            timing_score=0.8,
            stage="growth",
            action="open" if i == 0 else "exit",
            priority_score=0.8,
            weight_mode="manual",
            factor_model_run_id="model-integration",
            calc_batch_id=f"batch-{i}",
            published_at=datetime(2025, 1, 1, 0, 0),
        ))

    snapshot = StrategyExecutionSnapshot(
        id="snapshot-integration",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        factor_model_run_id="model-integration",
        decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id, "member_id": 1}]),
        snapshot_type="save_and_apply",
        snapshot_hash="hash-integration",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=datetime(2025, 1, 1),
    )
    db_session.add(snapshot)
    db_session.commit()

    run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=days[0],
        end_date=days[-1],
        rule_config={
            "buy_conditions": {"quality_score_min": 0.8},
            "sell_conditions": {"score_actions": ["exit"]},
            "position_config": {"type": "fixed_pct", "value": 0.5, "max_positions": 1},
        },
        initial_capital=portfolio.total_capital,
        strategy_snapshot_id=snapshot.id,
    )

    run = db_session.execute(
        select(BacktestRun).where(BacktestRun.strategy_snapshot_id == snapshot.id)
    ).scalar_one()
    decision_runs = db_session.execute(
        select(DecisionRun).where(DecisionRun.strategy_snapshot_id == snapshot.id).order_by(DecisionRun.trade_date)
    ).scalars().all()
    evidence = db_session.execute(
        select(DecisionEvidence).where(DecisionEvidence.strategy_snapshot_id == snapshot.id)
    ).scalars().all()
    trade = db_session.execute(select(BacktestTrade).where(BacktestTrade.run_id == run.id)).scalar_one()
    fills = db_session.execute(
        select(BacktestExecutionFill)
        .where(BacktestExecutionFill.run_id == run.id)
        .order_by(BacktestExecutionFill.execution_date, BacktestExecutionFill.id)
    ).scalars().all()

    assert run.status == "completed"
    assert json.loads(run.decision_run_ids_json) == [item.id for item in decision_runs]
    assert len(decision_runs) == len(days)
    assert all(item.status == "SUCCEEDED" and item.finished_at is not None for item in decision_runs)
    assert len(evidence) == len(days)
    assert trade.entry_date == days[1]
    assert trade.exit_date == days[2]
    assert trade.decision_evidence_id is not None
    assert trade.exit_evidence_id is not None
    assert db_session.get(DecisionEvidence, trade.decision_evidence_id).action == "BUY"
    assert db_session.get(DecisionEvidence, trade.exit_evidence_id).action == "SELL"
    entry_versions = json.loads(
        db_session.get(DecisionEvidence, trade.decision_evidence_id).versions_json or "{}"
    )
    assert entry_versions["order_plan_status"] == "FILLED"
    assert entry_versions["order_plan_requested_quantity"] == entry_versions["order_plan_filled_quantity"]
    assert [fill.side for fill in fills] == ["BUY", "SELL"]
    assert [fill.execution_date for fill in fills] == [days[1], days[2]]
    assert fills[0].backtest_trade_id == trade.id
    assert fills[0].decision_evidence_id == trade.decision_evidence_id
    assert fills[1].decision_evidence_id == trade.exit_evidence_id


def test_decision_engine_returns_stable_order_plans_for_each_evidence(db_session):
    portfolio = Portfolio(
        name="decision-plan-integration",
        account_type="simulated",
        total_capital=100_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        currency="CNY",
    )
    symbol = Symbol(symbol="600001", name="Plan Integration", market="SH", asset_type="stock")
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    trade_date = date(2025, 1, 6)
    db_session.add(DailyBar(
        symbol_id=symbol.id, trade_date=trade_date,
        open=10.0, high=10.2, low=9.8, close=10.0,
        volume=1_000_000, amount=10_000_000,
    ))
    db_session.add(Score(
        symbol_id=symbol.id, trade_date=trade_date,
        quality_score=0.9, quality_grade="A", timing_score=0.8,
        stage="growth", action="open", priority_score=0.8,
        weight_mode="manual", factor_model_run_id="model-plan",
        calc_batch_id="batch-plan", published_at=datetime(2025, 1, 1),
    ))
    snapshot = StrategyExecutionSnapshot(
        id="snapshot-plan",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        factor_model_run_id="model-plan",
        decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id, "member_id": 1}]),
        snapshot_type="save_and_apply", snapshot_hash="hash-plan",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=datetime(2025, 1, 1),
    )
    db_session.add(snapshot)
    db_session.commit()

    result = default_engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id,
        trade_date=trade_date,
        run_type="backtest",
        dry_run=True,
    )

    assert len(result.order_plans) == len(result.evidence)
    assert result.order_plans[0].decision_run_id == result.decision_run_id
    assert result.order_plans[0].evidence_id is not None
    assert result.order_plans[0].signal_date == trade_date
    assert result.order_plans[0].execution_date == result.clock.execution_at_sh.date()


def test_decision_driven_backtest_failure_is_traceable_and_has_no_partial_trade(
    db_session, monkeypatch,
):
    portfolio = Portfolio(
        name="decision-chain-failure",
        account_type="simulated",
        total_capital=100_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        currency="CNY",
    )
    symbol = Symbol(symbol="600002", name="Failure Integration", market="SH", asset_type="stock")
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    for i, trade_date in enumerate(days):
        close = 10.0 + i * 0.2
        db_session.add(DailyBar(
            symbol_id=symbol.id, trade_date=trade_date,
            open=close, high=close + 0.1, low=close - 0.1, close=close,
            volume=1_000_000, amount=close * 1_000_000,
        ))
        db_session.add(Score(
            symbol_id=symbol.id, trade_date=trade_date,
            quality_score=0.9 if i == 0 else 0.2,
            quality_grade="A", timing_score=0.8, stage="growth",
            action="open" if i == 0 else "exit", priority_score=0.8,
            weight_mode="manual", factor_model_run_id="model-failure",
            calc_batch_id=f"failure-batch-{i}", published_at=datetime(2025, 1, 1),
        ))
    snapshot = StrategyExecutionSnapshot(
        id="snapshot-failure",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        factor_model_run_id="model-failure",
        decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id, "member_id": 1}]),
        snapshot_type="save_and_apply",
        snapshot_hash="hash-failure",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=datetime(2025, 1, 1),
    )
    db_session.add(snapshot)
    db_session.commit()

    original_evaluate = default_engine.evaluate
    calls = {"count": 0}

    def flaky_evaluate(db, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected decision failure on day 2")
        return original_evaluate(db, **kwargs)

    monkeypatch.setattr(default_engine, "evaluate", flaky_evaluate)
    with pytest.raises(RuntimeError, match="injected decision failure"):
        run_backtest(
            db_session,
            portfolio_id=portfolio.id,
            symbol_ids=[symbol.id],
            start_date=days[0],
            end_date=days[-1],
            rule_config={"position_config": {"type": "fixed_pct", "value": 0.5}},
            initial_capital=portfolio.total_capital,
            strategy_snapshot_id=snapshot.id,
        )

    run = db_session.execute(
        select(BacktestRun).where(BacktestRun.strategy_snapshot_id == snapshot.id)
    ).scalar_one()
    decision_runs = db_session.execute(
        select(DecisionRun).where(DecisionRun.strategy_snapshot_id == snapshot.id)
    ).scalars().all()
    trades = db_session.execute(
        select(BacktestTrade).where(BacktestTrade.run_id == run.id)
    ).scalars().all()
    fills = db_session.execute(
        select(BacktestExecutionFill).where(BacktestExecutionFill.run_id == run.id)
    ).scalars().all()
    assert run.status == "failed"
    assert "injected decision failure" in (run.error_message or "")
    assert decision_runs
    assert all(item.status == "FAILED" for item in decision_runs)
    assert trades == []
    assert fills == []
