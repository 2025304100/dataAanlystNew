"""Decision-driven backtest partial-fill lifecycle contract."""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select

from app.api.routes.backtest import list_backtest_positions
from app.models.backtest import BacktestExecutionFill, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionEvidence, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.backtest import run_backtest


def test_partial_buy_retry_accumulates_the_original_plan_on_one_trade(db_session):
    """A volume-limited BUY retry must retain its plan/evidence identity.

    The public backtest entry point is responsible for scheduling the T+1
    retry.  The first execution fills 600 of the 1,000-share plan; the next
    session fills the remaining 400 shares.  Neither leg may be represented
    as a second entry trade or rejected because the first leg opened a
    position.
    """
    portfolio = Portfolio(
        name="partial-buy-lifecycle",
        account_type="simulated",
        total_capital=200_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.10,
    )
    symbol = Symbol(
        symbol="600031",
        name="Partial Lifecycle",
        market="SH",
        asset_type="stock",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()

    days = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
    volumes = [100_000.0, 1_200.0, 10_000.0]
    scores = [0.9, 0.5, 0.5]
    for trade_date, volume, quality_score in zip(days, volumes, scores):
        db_session.add(DailyBar(
            symbol_id=symbol.id,
            trade_date=trade_date,
            open=10.0,
            high=10.1,
            low=9.9,
            close=10.0,
            volume=volume,
            amount=10.0 * volume,
        ))
        db_session.add(Score(
            symbol_id=symbol.id,
            trade_date=trade_date,
            quality_score=quality_score,
            quality_grade="A",
            timing_score=0.8,
            stage="growth",
            action="open" if quality_score >= 0.7 else "hold",
            priority_score=0.8,
            weight_mode="manual",
            factor_model_run_id="model-partial-lifecycle",
            calc_batch_id=f"batch-{trade_date.isoformat()}",
            published_at=datetime(2025, 1, 1),
        ))

    snapshot = StrategyExecutionSnapshot(
        id="snapshot-partial-lifecycle",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        factor_model_run_id="model-partial-lifecycle",
        decision_clock_json="{}",
        member_snapshot_json=json.dumps([
            {"symbol_id": symbol.id, "member_id": 1},
        ]),
        snapshot_type="save_and_apply",
        snapshot_hash="hash-partial-lifecycle",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=datetime(2025, 1, 1),
    )
    db_session.add(snapshot)
    db_session.commit()

    run_kwargs = dict(
        db=db_session,
        portfolio_id=portfolio.id,
        symbol_ids=[symbol.id],
        start_date=days[0],
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.05}},
        initial_capital=portfolio.total_capital,
        strategy_snapshot_id=snapshot.id,
        cost_config={
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "stamp_tax_rate": 0.001,
            "transfer_fee_rate": 0.00001,
            "slippage_buy_bps": 0,
            "slippage_sell_bps": 0,
            "volume_limit_pct": 0.5,
        },
    )

    # First invocation ends after the constrained execution session. This
    # observes the durable partial state before a later replay is attempted.
    first_run = run_backtest(
        **run_kwargs,
        end_date=days[1],
    )
    first_trade = db_session.execute(
        select(BacktestTrade).where(BacktestTrade.run_id == first_run.id)
    ).scalar_one()
    first_evidence = db_session.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.strategy_snapshot_id == snapshot.id,
            DecisionEvidence.trade_date == days[0],
            DecisionEvidence.action == "BUY",
        )
    ).scalar_one()
    first_versions = json.loads(first_evidence.versions_json or "{}")
    assert first_trade.quantity == 600.0
    assert first_versions["order_plan_requested_quantity"] == 1_000.0
    assert first_versions["order_plan_filled_quantity"] == 600.0
    assert first_versions["order_plan_remaining_quantity"] == 400.0
    assert first_versions["order_plan_status"] == "PARTIAL_FILL"

    # A longer replay reuses idempotent DecisionRun/Evidence rows but must
    # execute a fresh BacktestTrade lifecycle and consume only the remainder.
    run = run_backtest(
        **run_kwargs,
        end_date=days[-1],
    )

    trades = db_session.execute(
        select(BacktestTrade)
        .where(BacktestTrade.run_id == run.id)
        .order_by(BacktestTrade.id)
    ).scalars().all()
    assert len(trades) == 1
    assert trades[0].entry_date == days[1]
    assert trades[0].quantity == 1_000.0

    buy_evidence = db_session.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.strategy_snapshot_id == snapshot.id,
            DecisionEvidence.trade_date == days[0],
            DecisionEvidence.action == "BUY",
        )
    ).scalar_one()
    assert trades[0].decision_evidence_id == buy_evidence.id
    assert buy_evidence.rejection_reason is None
    assert "POSITION_ALREADY_OPEN" not in (buy_evidence.rejection_detail or "")

    versions = json.loads(buy_evidence.versions_json or "{}")
    assert versions["order_plan_status"] == "FILLED"
    assert versions["order_plan_requested_quantity"] == 1_000.0
    assert versions["order_plan_filled_quantity"] == 1_000.0
    assert versions["order_plan_remaining_quantity"] == 0.0

    # The public holding-ledger API must retain the actual two-session fill
    # history.  The aggregate BacktestTrade stays at 1,000 shares, but it is
    # not an execution-event source: querying through the first execution
    # session may only expose the 600 shares that had actually filled then.
    fills = db_session.execute(
        select(BacktestExecutionFill)
        .where(BacktestExecutionFill.run_id == run.id)
        .order_by(BacktestExecutionFill.execution_date, BacktestExecutionFill.id)
    ).scalars().all()
    assert [
        (fill.execution_date, fill.side, fill.quantity, fill.decision_evidence_id)
        for fill in fills
    ] == [
        (days[1], "BUY", 600.0, buy_evidence.id),
        (days[2], "BUY", 400.0, buy_evidence.id),
    ]
    assert {fill.order_plan_id for fill in fills} == {versions["order_plan_id"]}

    first_session_page = list_backtest_positions(
        run.id,
        page=1,
        page_size=20,
        as_of_date=days[1],
        status=None,
        db=db_session,
    )
    assert first_session_page["ledger_mode"] == "EXECUTION_EVENTS"
    assert first_session_page["items"] == [
        {
            **first_session_page["items"][0],
            "trade_date": days[1].isoformat(),
            "opening_quantity": 0.0,
            "buy_quantity": 600.0,
            "sell_quantity": 0.0,
            "closing_quantity": 600.0,
            "buy_evidence_ids": [buy_evidence.id],
            "sell_evidence_ids": [],
        }
    ]

    final_session_page = list_backtest_positions(
        run.id,
        page=1,
        page_size=20,
        as_of_date=days[2],
        status=None,
        db=db_session,
    )
    final_rows = {
        row["trade_date"]: row for row in final_session_page["items"]
    }
    assert final_session_page["ledger_mode"] == "EXECUTION_EVENTS"
    assert final_rows[days[1].isoformat()]["buy_quantity"] == 600.0
    assert final_rows[days[1].isoformat()]["closing_quantity"] == 600.0
    assert final_rows[days[2].isoformat()]["opening_quantity"] == 600.0
    assert final_rows[days[2].isoformat()]["buy_quantity"] == 400.0
    assert final_rows[days[2].isoformat()]["closing_quantity"] == 1_000.0

    # Net value is reconstructed from the same events, so cash only reflects
    # the first 600-share execution on the first fill session.
    equity_by_date = {
        point["date"]: point for point in json.loads(run.equity_curve_json or "[]")
    }
    assert equity_by_date[days[1].isoformat()]["cash"] == 193_994.94
    assert equity_by_date[days[2].isoformat()]["cash"] == 189_989.90
