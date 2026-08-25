"""Stage-2 daily-bar point-in-time contracts at public decision seams."""
from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd
from sqlalchemy import select

from app.api.routes.backtest import list_backtest_positions
from app.models.backtest import BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionEvidence, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.backtest import run_backtest
from app.services.decision_engine import DecisionStateContext, default_engine
from app.services import market_data


def _seed_strict_snapshot(db_session, *, snapshot_id: str):
    portfolio = Portfolio(
        name=f"daily-bar-pit-{snapshot_id}",
        account_type="simulated",
        total_capital=100_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        currency="CNY",
    )
    symbol = Symbol(
        symbol=f"600{snapshot_id[-3:]}",
        name="Daily Bar PIT",
        market="SH",
        asset_type="stock",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()

    snapshot = StrategyExecutionSnapshot(
        id=snapshot_id,
        snapshot_no=1,
        portfolio_id=portfolio.id,
        factor_model_run_id=f"model-{snapshot_id}",
        decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id, "member_id": 1}]),
        snapshot_type="save_and_apply",
        snapshot_hash=f"hash-{snapshot_id}",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({
            "run_mode": "production_pit",
            "pit_mode": "strict_pit_safe",
        }),
        effective_from=datetime(2025, 1, 1),
    )
    db_session.add(snapshot)
    return portfolio, symbol, snapshot


def _add_buy_score(db_session, *, symbol_id: int, model_id: str, trade_date: date):
    db_session.add(Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=0.9,
        quality_grade="A",
        timing_score=0.8,
        stage="growth",
        action="open",
        priority_score=0.9,
        weight_mode="manual",
        factor_model_run_id=model_id,
        calc_batch_id=f"score-{trade_date.isoformat()}",
        published_at=datetime(2025, 1, 1),
        pit_safety="PIT_VERIFIED",
    ))


def test_production_evaluate_blocks_daily_bar_available_after_cutoff(db_session):
    portfolio, symbol, snapshot = _seed_strict_snapshot(
        db_session, snapshot_id="snapshot-daily-evaluate",
    )
    decision_date = date(2025, 1, 6)
    _add_buy_score(
        db_session,
        symbol_id=symbol.id,
        model_id=snapshot.factor_model_run_id,
        trade_date=decision_date,
    )
    db_session.commit()

    result = default_engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id,
        trade_date=decision_date,
        run_type="backtest",
        dry_run=True,
        state_context=DecisionStateContext(
            available_cash=100_000.0,
            total_capital=100_000.0,
            price_data_by_symbol={
                symbol.id: {
                    "open_price": 10.0,
                    "close_price": 10.0,
                    "high_price": 10.2,
                    "low_price": 9.8,
                    "volume": 1_000_000,
                    # 07:00 UTC is the 15:00 Shanghai decision cutoff.
                    "available_at": datetime(2025, 1, 6, 7, 1),
                }
            },
        ),
    )

    evidence = result.evidence[0]
    assert evidence.action == "HOLD"
    assert evidence.action_subtype == "DATA_BLOCKED"
    assert evidence.target_qty_delta == 0.0
    assert evidence.executed_price is None
    assert "DAILY_BAR_AVAILABLE_AFTER_CUTOFF" in (evidence.blocking_reason or "")
    assert "DAILY_BAR_AVAILABLE_AFTER_CUTOFF" in evidence.reason_codes


def test_production_evaluate_fails_closed_without_price_availability_proof(db_session):
    portfolio, symbol, snapshot = _seed_strict_snapshot(
        db_session, snapshot_id="snapshot-daily-missing-availability",
    )
    decision_date = date(2025, 1, 6)
    _add_buy_score(
        db_session,
        symbol_id=symbol.id,
        model_id=snapshot.factor_model_run_id,
        trade_date=decision_date,
    )
    db_session.commit()

    result = default_engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id,
        trade_date=decision_date,
        run_type="backtest",
        dry_run=True,
        state_context=DecisionStateContext(
            available_cash=100_000.0,
            total_capital=100_000.0,
            price_data_by_symbol={
                symbol.id: {
                    "open_price": 10.0,
                    "close_price": 10.0,
                    "high_price": 10.2,
                    "low_price": 9.8,
                    "volume": 1_000_000,
                }
            },
        ),
    )

    evidence = result.evidence[0]
    assert evidence.action == "HOLD"
    assert evidence.action_subtype == "DATA_BLOCKED"
    assert evidence.target_qty_delta == 0.0
    assert "PRICE_DATA_AVAILABILITY_MISSING" in (evidence.blocking_reason or "")
    assert "PRICE_DATA_AVAILABILITY_MISSING" in evidence.reason_codes


def test_strict_pit_backtest_does_not_trade_future_inserted_daily_bar(db_session):
    portfolio, symbol, snapshot = _seed_strict_snapshot(
        db_session, snapshot_id="snapshot-daily-backtest",
    )
    signal_date = date(2025, 1, 6)
    execution_date = date(2025, 1, 7)
    _add_buy_score(
        db_session,
        symbol_id=symbol.id,
        model_id=snapshot.factor_model_run_id,
        trade_date=signal_date,
    )
    # This is a late historical insert for the signal day. Its extreme value
    # must not become a past decision input in a strict PIT run.
    db_session.add(DailyBar(
        symbol_id=symbol.id,
        trade_date=signal_date,
        open=9_999.0,
        high=10_000.0,
        low=9_998.0,
        close=9_999.0,
        volume=1_000_000,
        amount=9_999_000_000.0,
        created_at=datetime(2025, 1, 6, 7, 1),
    ))
    db_session.add(DailyBar(
        symbol_id=symbol.id,
        trade_date=execution_date,
        open=10.0,
        high=10.2,
        low=9.8,
        close=10.0,
        volume=1_000_000,
        amount=10_000_000.0,
        created_at=datetime(2025, 1, 7, 1, 29),
    ))
    db_session.commit()

    run = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=signal_date,
        end_date=execution_date,
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.5}},
        initial_capital=100_000.0,
        strategy_snapshot_id=snapshot.id,
    )

    assert run.status == "completed"
    assert db_session.execute(
        select(BacktestTrade).where(BacktestTrade.run_id == run.id)
    ).scalars().all() == []
    evidence = db_session.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.strategy_snapshot_id == snapshot.id,
            DecisionEvidence.trade_date == signal_date,
        )
    ).scalar_one()
    assert evidence.action == "HOLD"
    assert evidence.action_subtype == "DATA_BLOCKED"
    assert "DAILY_BAR_AVAILABLE_AFTER_CUTOFF" in (evidence.blocking_reason or "")


def test_strict_pit_backtest_rejects_late_execution_bar_revision(db_session):
    portfolio, symbol, snapshot = _seed_strict_snapshot(
        db_session, snapshot_id="snapshot-daily-execution",
    )
    signal_date = date(2025, 1, 6)
    execution_date = date(2025, 1, 7)
    _add_buy_score(
        db_session,
        symbol_id=symbol.id,
        model_id=snapshot.factor_model_run_id,
        trade_date=signal_date,
    )
    db_session.add(DailyBar(
        symbol_id=symbol.id,
        trade_date=signal_date,
        open=10.0,
        high=10.2,
        low=9.8,
        close=10.0,
        volume=1_000_000,
        amount=10_000_000.0,
        created_at=datetime(2025, 1, 6, 6, 59),
    ))
    # A revision appearing after the T+1 open cannot be used for an opening
    # fill, even though it has the historical execution date.
    db_session.add(DailyBar(
        symbol_id=symbol.id,
        trade_date=execution_date,
        open=10.5,
        high=10.7,
        low=10.3,
        close=10.5,
        volume=1_000_000,
        amount=10_500_000.0,
        created_at=datetime(2025, 1, 7, 7, 1),
    ))
    db_session.commit()

    run = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=signal_date,
        end_date=execution_date,
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.5}},
        initial_capital=100_000.0,
        strategy_snapshot_id=snapshot.id,
    )

    assert run.status == "completed"
    assert db_session.execute(
        select(BacktestTrade).where(BacktestTrade.run_id == run.id)
    ).scalars().all() == []
    evidence = db_session.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.strategy_snapshot_id == snapshot.id,
            DecisionEvidence.trade_date == signal_date,
        )
    ).scalar_one()
    assert evidence.rejection_reason == "DAILY_BAR_AVAILABLE_AFTER_EXECUTION_CUTOFF"
    assert evidence.executed_price is None


def test_strict_pit_backtest_blocks_late_source_upsert_revision(db_session):
    portfolio, symbol, snapshot = _seed_strict_snapshot(
        db_session, snapshot_id="snapshot-daily-source-revision",
    )
    signal_date = date(2025, 1, 6)
    execution_date = date(2025, 1, 7)
    _add_buy_score(
        db_session,
        symbol_id=symbol.id,
        model_id=snapshot.factor_model_run_id,
        trade_date=signal_date,
    )
    db_session.add_all([
        DailyBar(
            symbol_id=symbol.id,
            trade_date=signal_date,
            open=10.0,
            high=10.2,
            low=9.8,
            close=10.0,
            volume=1_000_000,
            amount=10_000_000.0,
            created_at=datetime(2025, 1, 6, 6, 59),
        ),
        DailyBar(
            symbol_id=symbol.id,
            trade_date=execution_date,
            open=10.0,
            high=10.2,
            low=9.8,
            close=10.0,
            volume=1_000_000,
            amount=10_000_000.0,
            created_at=datetime(2025, 1, 7, 1, 29),
        ),
    ])
    db_session.commit()

    # The normal production source mutates a same-day row when a vendor
    # correction arrives. The current payload must become newly available,
    # otherwise the old creation time makes the correction look historical.
    inserted, updated = market_data._upsert_bars(
        db_session,
        symbol,
        pd.DataFrame([{
            "trade_date": signal_date,
            "open": 9_999.0,
            "high": 10_000.0,
            "low": 9_998.0,
            "close": 9_999.0,
            "volume": 1_000_000,
            "amount": 9_999_000_000.0,
            "turnover_rate": 1.0,
        }]),
    )
    assert (inserted, updated) == (0, 1)
    db_session.commit()

    run = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=signal_date,
        end_date=execution_date,
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.5}},
        initial_capital=100_000.0,
        strategy_snapshot_id=snapshot.id,
    )

    assert run.status == "completed"
    assert db_session.execute(
        select(BacktestTrade).where(BacktestTrade.run_id == run.id)
    ).scalars().all() == []
    evidence = db_session.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.strategy_snapshot_id == snapshot.id,
            DecisionEvidence.trade_date == signal_date,
        )
    ).scalar_one()
    assert evidence.action_subtype == "DATA_BLOCKED"
    assert "DAILY_BAR_AVAILABLE_AFTER_CUTOFF" in (evidence.blocking_reason or "")


def test_completed_backtest_position_valuation_does_not_drift_after_bar_revision(db_session):
    """The public holding ledger must read the price locked by the completed run."""
    portfolio, symbol, snapshot = _seed_strict_snapshot(
        db_session, snapshot_id="snapshot-daily-valuation-freeze",
    )
    signal_date = date(2025, 1, 6)
    execution_date = date(2025, 1, 7)
    _add_buy_score(
        db_session,
        symbol_id=symbol.id,
        model_id=snapshot.factor_model_run_id,
        trade_date=signal_date,
    )
    db_session.add_all([
        DailyBar(
            symbol_id=symbol.id,
            trade_date=signal_date,
            open=10.0,
            high=10.2,
            low=9.8,
            close=10.0,
            volume=1_000_000,
            amount=10_000_000.0,
            created_at=datetime(2025, 1, 6, 6, 59),
        ),
        DailyBar(
            symbol_id=symbol.id,
            trade_date=execution_date,
            open=10.0,
            high=10.2,
            low=9.8,
            close=10.0,
            volume=1_000_000,
            amount=10_000_000.0,
            created_at=datetime(2025, 1, 7, 1, 29),
        ),
    ])
    db_session.commit()

    run = run_backtest(
        db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=signal_date,
        end_date=execution_date,
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.5}},
        initial_capital=100_000.0,
        strategy_snapshot_id=snapshot.id,
    )
    baseline = list_backtest_positions(
        run.id,
        page=1,
        page_size=20,
        as_of_date=execution_date,
        status="OPEN",
        db=db_session,
    )
    assert baseline["items"]
    baseline_row = baseline["items"][0]
    assert baseline_row["mark_price"] == 10.0

    # This models a vendor correction applied after the backtest is complete.
    revised_bar = db_session.execute(select(DailyBar).where(
        DailyBar.symbol_id == symbol.id,
        DailyBar.trade_date == execution_date,
    )).scalar_one()
    revised_bar.close = 99.0
    db_session.commit()

    reread = list_backtest_positions(
        run.id,
        page=1,
        page_size=20,
        as_of_date=execution_date,
        status="OPEN",
        db=db_session,
    )
    assert reread["items"][0]["mark_price"] == baseline_row["mark_price"]
    assert reread["items"][0]["market_value"] == baseline_row["market_value"]
