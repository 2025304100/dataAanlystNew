"""Public backtest/ledger regressions for the personal-workbench P0 fixes."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.routes.backtest import list_backtest_trades
from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionRun, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.backtest import run_backtest


pytestmark = pytest.mark.whitebox


def _scenario(db, bars, *, board="main", start_day=date(2025, 1, 6)):
    portfolio = Portfolio(
        name="p0-execution", account_type="simulated", total_capital=200_000,
        investable_ratio=1, cash_reserve_ratio=0, currency="CNY",
        default_single_position_pct=0.10,
    )
    symbol = Symbol(
        symbol="600038" if board == "main" else "300038", name="P0 execution",
        market="SH" if board == "main" else "SZ", asset_type="stock", board=board,
    )
    db.add_all([portfolio, symbol])
    db.flush()
    days = [start_day + timedelta(days=i) for i in range(len(bars))]
    available_at = datetime(start_day.year, start_day.month, start_day.day) - timedelta(days=5)
    for day, (open_price, close_price, volume, alpha) in zip(days, bars):
        db.add(DailyBar(
            symbol_id=symbol.id, trade_date=day, open=open_price,
            high=max(open_price, close_price) + 0.1,
            low=min(open_price, close_price) - 0.1, close=close_price,
            volume=volume, amount=close_price * volume,
            created_at=available_at,
        ))
        db.add(Score(
            symbol_id=symbol.id, trade_date=day, quality_score=alpha,
            quality_grade="A", timing_score=80, stage="growth",
            action="open" if alpha >= 70 else "exit" if alpha < 30 else "hold",
            priority_score=80, weight_mode="ridge", factor_model_run_id="model-p0",
            model_alpha_score=alpha, factor_data_cutoff_at=available_at,
            calc_batch_id=f"p0-{day}", published_at=available_at,
        ))
    from tests.historical_backtest_helpers import seed_daily_history
    seed_daily_history(db, symbol.id, days)
    snapshot = StrategyExecutionSnapshot(
        id=f"p0-snapshot-{portfolio.id}", snapshot_no=1, portfolio_id=portfolio.id,
        factor_model_run_id="model-p0", decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id, "member_id": 1}]),
        snapshot_type="save_and_apply", snapshot_hash=f"p0-hash-{portfolio.id}",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=available_at,
    )
    db.add(snapshot)
    db.commit()
    return dict(
        db=db, portfolio_id=portfolio.id, symbol_ids=[symbol.id],
        start_date=days[0], end_date=days[-1], strategy_snapshot_id=snapshot.id,
        initial_capital=200_000,
        rule_config={"position_config": {"type": "fixed_pct", "value": 0.05}},
        cost_config={
            "commission_rate": 0, "min_commission": 0, "stamp_tax_rate": 0,
            "transfer_fee_rate": 0, "slippage_buy_bps": 0, "slippage_sell_bps": 0,
        },
    )


def _ledger(db, run_id):
    return list_backtest_trades(
        run_id, page=1, page_size=200, limit=None, offset=0, action=None,
        symbol_id=None, start_date=None, end_date=None, execution_status=None,
        sort_by="execution_at", sort_dir="asc", db=db,
    )["items"]


def test_after_close_order_quantity_uses_signal_close_not_tomorrows_open(db_session):
    kwargs = _scenario(db_session, [
        (9.5, 10.0, 100_000, 90),
        (10.5, 10.5, 100_000, 20),
        (10.5, 10.5, 100_000, 20),
    ])
    run = run_backtest(**kwargs)
    rows = _ledger(db_session, run.id)
    assert len(rows) == 1
    # The known T close is 10: a 10,000 CNY target means 1,000 shares.
    # Tomorrow's 10.5 opening price may affect the fill, never tonight's size.
    assert rows[0]["quantity"] == 1_000
    assert rows[0]["entry_price"] == 10.5


def test_partial_exit_allocates_entry_cost_once_and_reconciles_to_cash(db_session):
    kwargs = _scenario(db_session, [
        (10, 10, 100_000, 90),
        (10, 10, 100_000, 20),
        (10, 10, 1_000, 20),
        (10, 10, 100_000, 20),
    ])
    kwargs["cost_config"].update(commission_rate=0.001, volume_limit_pct=0.5)
    run = run_backtest(**kwargs)
    rows = _ledger(db_session, run.id)
    assert [row["quantity"] for row in rows] == [500, 500]
    # Flat prices: 10 CNY to enter, then 5 + 5 CNY to exit. Each half
    # receives 5 CNY entry cost; realized PnL must agree with the cash ledger.
    assert [row["entry_cost"] for row in rows] == [5, 5]
    assert [row["pnl"] for row in rows] == [-10, -10]
    assert json.loads(run.equity_curve_json)[-1]["cash"] == 199_980


def test_chinext_gap_does_not_get_an_artificial_main_board_fill_price(db_session):
    kwargs = _scenario(db_session, [
        (10, 10, 100_000, 90),
        (11.5, 11.5, 100_000, 20),
        (11.5, 11.5, 100_000, 20),
    ], board="chinext")
    run = run_backtest(**kwargs)
    rows = _ledger(db_session, run.id)
    assert len(rows) == 1
    assert rows[0]["entry_price"] == 11.5


def test_backtest_auto_tax_uses_execution_date_without_overriding_user_commission(db_session):
    kwargs = _scenario(db_session, [
        (10, 10, 100_000, 90),
        (10, 10, 100_000, 20),
        (10, 10, 100_000, 20),
    ])
    kwargs["cost_config"].pop("stamp_tax_rate")
    kwargs["cost_config"]["commission_rate"] = 0.001
    run = run_backtest(**kwargs)
    rows = _ledger(db_session, run.id)
    # A 10,000 CNY sale in 2025: commission 10, stamp tax 5.
    assert rows[0]["entry_cost"] == 10
    assert rows[0]["exit_cost"] == 15
    assert rows[0]["pnl"] == -25


def test_replay_does_not_reuse_decisions_from_the_old_price_reference_policy(db_session):
    kwargs = _scenario(db_session, [
        (9.5, 10, 100_000, 90),
        (10.5, 10.5, 100_000, 20),
        (10.5, 10.5, 100_000, 20),
    ])
    # Arrange a previously successful, pre-fix decision with no current
    # price-reference-policy marker. It must remain audit history only.
    db_session.add(DecisionRun(
        id="legacy-next-open-sizing", strategy_snapshot_id=kwargs["strategy_snapshot_id"],
        portfolio_id=kwargs["portfolio_id"], run_type="backtest", status="SUCCEEDED",
        trade_date=kwargs["start_date"], decision_at=datetime(2025, 1, 6, 7, 5),
        data_cutoff_at=datetime(2025, 1, 6, 7), execution_at=datetime(2025, 1, 7, 1, 30),
        versions_json=json.dumps({"decision_score_policy_version": "factor-score-v2"}),
    ))
    db_session.commit()
    run = run_backtest(**kwargs)
    rows = _ledger(db_session, run.id)
    assert len(rows) == 1
    assert rows[0]["quantity"] == 1_000
    assert "legacy-next-open-sizing" not in json.loads(run.decision_run_ids_json)


def test_legacy_backtest_auto_tax_uses_historical_execution_day(db_session):
    kwargs = _scenario(db_session, [
        (10, 10, 100_000, 90),
        (10, 10, 100_000, 20),
        (10, 10, 100_000, 20),
    ], start_day=date(2023, 8, 23))
    kwargs.pop("strategy_snapshot_id")
    kwargs["score_weight_mode"] = "manual"
    kwargs["cost_config"].pop("stamp_tax_rate")
    kwargs["cost_config"].update(commission_rate=0.001, slippage_rate=0)
    kwargs["rule_config"].update(
        buy_conditions={"quality_score_min": 80},
        sell_conditions={"score_actions": ["exit"]},
        execution_config={"entry_timing": "next_open", "exit_timing": "next_open"},
    )
    for score in db_session.scalars(select(Score).where(Score.symbol_id.in_(kwargs["symbol_ids"]))):
        score.weight_mode = "manual"
    db_session.commit()
    run = run_backtest(**kwargs)
    rows = _ledger(db_session, run.id)
    assert len(rows) == 1
    # 2023-08-25 precedes the cut: 10 commission + 10 stamp tax.
    assert rows[0]["exit_cost"] == 20
