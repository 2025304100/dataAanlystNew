"""Stage-1 failure and idempotency contracts for snapshot-backed backtests."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.models.backtest import BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.backtest import run_backtest
from app.services.decision_engine import default_engine


def _seed_snapshot_case(db_session):
    portfolio = Portfolio(
        name="stage1-idempotency",
        account_type="simulated",
        total_capital=100_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        currency="CNY",
    )
    symbol = Symbol(symbol="600701", name="Idempotency", market="SH", asset_type="stock")
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    for index, trade_date in enumerate(days):
        price = 10.0 + index * 0.1
        db_session.add(DailyBar(
            symbol_id=symbol.id,
            trade_date=trade_date,
            open=price,
            high=price + 0.1,
            low=price - 0.1,
            close=price,
            volume=1_000_000,
            amount=price * 1_000_000,
        ))
        db_session.add(Score(
            symbol_id=symbol.id,
            trade_date=trade_date,
            quality_score=0.9,
            quality_grade="A",
            timing_score=0.8,
            stage="growth",
            action="open",
            priority_score=0.8,
            weight_mode="manual",
            factor_model_run_id="stage1-idempotency-model",
            calc_batch_id=f"stage1-idempotency-{index}",
            published_at=datetime(2026, 1, 1),
        ))
    snapshot = StrategyExecutionSnapshot(
        id="stage1-idempotency-snapshot",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        factor_model_run_id="stage1-idempotency-model",
        decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id, "member_id": 1}]),
        snapshot_type="save_and_apply",
        snapshot_hash="stage1-idempotency-hash",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=datetime(2026, 1, 1),
    )
    db_session.add(snapshot)
    db_session.commit()
    return portfolio, symbol, snapshot, days


def test_failed_replay_does_not_downgrade_existing_decision_run(db_session, monkeypatch):
    portfolio, symbol, snapshot, days = _seed_snapshot_case(db_session)

    # Establish an idempotent, already-successful decision for the first day.
    # The replay extends into a new day so the injected failure still exercises
    # the transaction path rather than the successful-replay short-circuit.
    run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=days[0],
        end_date=days[0],
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.1}},
        initial_capital=portfolio.total_capital,
        strategy_snapshot_id=snapshot.id,
    )
    existing = {
        row.trade_date: row
        for row in db_session.execute(
            select(DecisionRun).where(
                DecisionRun.strategy_snapshot_id == snapshot.id,
                DecisionRun.trade_date.in_([days[0]]),
            )
        ).scalars().all()
    }
    assert set(existing) == {days[0]}
    assert all(row.status == "SUCCEEDED" for row in existing.values())

    original_evaluate = default_engine.evaluate

    def fail_on_second_day(db, **kwargs):
        if kwargs.get("trade_date") == days[1]:
            raise RuntimeError("stage1 injected replay failure")
        return original_evaluate(db, **kwargs)

    monkeypatch.setattr(default_engine, "evaluate", fail_on_second_day)
    with pytest.raises(RuntimeError, match="stage1 injected replay failure"):
        run_backtest(
            db_session,
            portfolio_id=portfolio.id,
            symbol_ids=[symbol.id],
            start_date=days[0],
            end_date=days[1],
            rule_config={"position_config": {"type": "fixed_pct", "value": 0.1}},
            initial_capital=portfolio.total_capital,
            strategy_snapshot_id=snapshot.id,
        )

    db_session.expire_all()
    assert all(
        db_session.get(DecisionRun, row.id).status == "SUCCEEDED"
        for row in existing.values()
    )
    failed_run = db_session.execute(
        select(BacktestRun).where(
            BacktestRun.strategy_snapshot_id == snapshot.id,
            BacktestRun.start_date == days[0],
            BacktestRun.end_date == days[1],
            BacktestRun.status == "failed",
        )
    ).scalar_one()
    assert failed_run.status == "failed"
    assert "stage1 injected replay failure" in (failed_run.error_message or "")


def test_successful_decision_runs_are_reused_without_re_evaluation(db_session, monkeypatch):
    """A replay changes execution inputs, but must reuse persisted decisions.

    The replay owns a fresh BacktestRun/transaction lifecycle.  It must not
    call the decision engine again for a successful (snapshot, trade_date)
    pair, and it must not borrow trades from the first run.
    """
    portfolio, symbol, snapshot, days = _seed_snapshot_case(db_session)
    first = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=days[0],
        end_date=days[1],
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.1}},
        initial_capital=portfolio.total_capital,
        strategy_snapshot_id=snapshot.id,
    )

    calls = {"count": 0}

    def unexpected_re_evaluation(*_args, **_kwargs):
        calls["count"] += 1
        raise AssertionError("successful snapshot/date was re-evaluated")

    monkeypatch.setattr(default_engine, "evaluate", unexpected_re_evaluation)
    second = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=days[0],
        end_date=days[1],
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.1}},
        # Execution-only parameters differ from the first run.
        initial_capital=portfolio.total_capital * 2,
        cost_config={
            "commission_rate": 0.001,
            "min_commission": 0.0,
            "stamp_tax_rate": 0.0,
            "transfer_fee_rate": 0.0,
            "slippage_buy_bps": 25,
            "slippage_sell_bps": 25,
        },
        strategy_snapshot_id=snapshot.id,
    )

    assert calls["count"] == 0
    assert second.status == "completed"
    assert second.id != first.id
    assert json.loads(second.decision_run_ids_json or "[]") == json.loads(
        first.decision_run_ids_json or "[]"
    )
    first_trade_ids = {
        row.id for row in db_session.execute(
            select(BacktestTrade).where(BacktestTrade.run_id == first.id)
        ).scalars()
    }
    second_trade_ids = {
        row.id for row in db_session.execute(
            select(BacktestTrade).where(BacktestTrade.run_id == second.id)
        ).scalars()
    }
    assert first_trade_ids
    assert second_trade_ids
    assert first_trade_ids.isdisjoint(second_trade_ids)


def test_retry_after_failure_restores_reused_decisions_to_succeeded(db_session, monkeypatch):
    """A completed retry must never reference a DecisionRun left FAILED."""
    portfolio, symbol, snapshot, days = _seed_snapshot_case(db_session)
    original_evaluate = default_engine.evaluate
    calls = {"count": 0}

    def fail_on_second_evaluation(db, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("retry-contract injected failure")
        return original_evaluate(db, **kwargs)

    monkeypatch.setattr(default_engine, "evaluate", fail_on_second_evaluation)
    with pytest.raises(RuntimeError, match="retry-contract injected failure"):
        run_backtest(
            db_session,
            portfolio_id=portfolio.id,
            symbol_ids=[symbol.id],
            start_date=days[0],
            end_date=days[1],
            rule_config={"position_config": {"type": "fixed_pct", "value": 0.1}},
            initial_capital=portfolio.total_capital,
            strategy_snapshot_id=snapshot.id,
        )

    assert {
        row.status for row in db_session.execute(
            select(DecisionRun).where(DecisionRun.strategy_snapshot_id == snapshot.id)
        ).scalars()
    } == {"FAILED"}

    monkeypatch.setattr(default_engine, "evaluate", original_evaluate)
    retry = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=days[0],
        end_date=days[1],
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.1}},
        initial_capital=portfolio.total_capital,
        strategy_snapshot_id=snapshot.id,
    )

    assert retry.status == "completed"
    retry_decision_ids = set(json.loads(retry.decision_run_ids_json or "[]"))
    retry_decisions = [db_session.get(DecisionRun, run_id) for run_id in retry_decision_ids]
    assert retry_decisions
    assert all(row is not None and row.status == "SUCCEEDED" for row in retry_decisions)


def test_replay_preserves_original_plan_reason_when_evidence_has_match_outcome(db_session):
    """A previous matching outcome cannot overwrite the immutable sell intent."""
    portfolio, symbol, snapshot, days = _seed_snapshot_case(db_session)
    exit_score = db_session.execute(
        select(Score).where(
            Score.symbol_id == symbol.id,
            Score.trade_date == days[1],
        )
    ).scalar_one()
    exit_score.quality_score = 0.2
    exit_score.action = "exit"
    db_session.commit()

    first = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=days[0],
        end_date=days[-1],
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.1}},
        initial_capital=portfolio.total_capital,
        strategy_snapshot_id=snapshot.id,
    )
    first_exit_evidence = db_session.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.strategy_snapshot_id == snapshot.id,
            DecisionEvidence.trade_date == days[1],
            DecisionEvidence.action == "SELL",
        )
    ).scalar_one()
    first_exit_evidence.rejection_reason = "LIMIT_DOWN_LOCKED"
    db_session.commit()

    replay = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=days[0],
        end_date=days[-1],
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.1}},
        initial_capital=portfolio.total_capital * 2,
        strategy_snapshot_id=snapshot.id,
    )
    replay_trade = db_session.execute(
        select(BacktestTrade).where(BacktestTrade.run_id == replay.id)
    ).scalar_one()

    assert first.id != replay.id
    assert replay_trade.exit_reason == "SELL_STRATEGY_EXIT"
