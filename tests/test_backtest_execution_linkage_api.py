"""HTTP contracts for exact backtest evidence linkage."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from app.api.routes.backtest import (
    _backtest_decision_context,
    list_backtest_positions,
    list_backtest_evidence,
    list_backtest_trades,
    router,
)
from app.models.backtest import BacktestRun, BacktestTrade
from app.models.decision_engine import (
    DecisionEvidence,
    DecisionOrderPlanRecord,
    DecisionRun,
    StrategyExecutionSnapshot,
)
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.models.daily_bar import DailyBar


pytestmark = pytest.mark.whitebox


def _seed_backtest_evidence(db_session) -> tuple[BacktestRun, dict[str, str]]:
    timestamp = datetime(2026, 8, 21, 15, 5)
    portfolio = Portfolio(
        name="API exact evidence linkage",
        account_type="simulated",
        total_capital=100_000.0,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
    )
    symbol = Symbol(
        symbol="API_LINKAGE_01",
        name="API Linkage",
        market="SH",
        asset_type="stock",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()

    snapshot = StrategyExecutionSnapshot(
        id="api-linkage-snapshot",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        decision_clock_json="{}",
        member_snapshot_json="[]",
        snapshot_hash="a" * 64,
        effective_from=timestamp,
    )
    db_session.add(snapshot)
    db_session.flush()

    run_dates = {
        "entry": date(2026, 8, 3),
        "exit": date(2026, 8, 7),
        "distractor": date(2026, 8, 4),
        "rejected_buy": date(2026, 8, 8),
        "rejected_sell": date(2026, 8, 9),
        "filled_buy": date(2026, 8, 10),
        "direct_rejected": date(2026, 8, 11),
        "data_blocked": date(2026, 8, 12),
    }
    decision_runs: dict[str, DecisionRun] = {}
    for name, trade_date in run_dates.items():
        decision_run = DecisionRun(
            id=f"api-linkage-{name}",
            strategy_snapshot_id=snapshot.id,
            portfolio_id=portfolio.id,
            run_type="backtest",
            status="SUCCEEDED",
            trade_date=trade_date,
            decision_at=timestamp + timedelta(days=(trade_date - run_dates["entry"]).days),
            data_cutoff_at=timestamp + timedelta(
                days=(trade_date - run_dates["entry"]).days,
                minutes=-5,
            ),
            execution_at=timestamp + timedelta(days=(trade_date - run_dates["entry"]).days + 1),
            universe_count=1,
            member_count=1,
        )
        decision_runs[name] = decision_run
        db_session.add(decision_run)
    db_session.flush()

    evidence_specs = {
        "entry": (
            "api-evidence-entry-exact",
            "BUY",
            {
                "order_plan_status": "PARTIAL_FILL_PENDING",
                "order_plan_requested_quantity": 300.0,
                "order_plan_filled_quantity": 100.0,
                "order_plan_remaining_quantity": 200.0,
                "order_plan_unfilled_reason": "VOLUME_LIMIT",
            },
        ),
        "exit": (
            "api-evidence-exit-exact",
            "SELL",
            {
                "order_plan_status": "FILLED",
                "order_plan_requested_quantity": 100.0,
                "order_plan_filled_quantity": 100.0,
                "order_plan_remaining_quantity": 0.0,
            },
        ),
        "distractor": ("api-evidence-same-symbol-distractor", "BUY", {}),
        "rejected_buy": (
            "api-evidence-execution-rejected-buy",
            "BUY",
            {"order_plan_status": "REJECTED"},
        ),
        "rejected_sell": (
            "api-evidence-execution-rejected-sell",
            "SELL",
            {"order_plan_status": "REJECTED"},
        ),
        "filled_buy": (
            "api-evidence-execution-filled-buy",
            "BUY",
            {"order_plan_status": "FILLED"},
        ),
        "direct_rejected": ("api-evidence-direct-rejected", "REJECTED", {}),
        "data_blocked": ("api-evidence-data-blocked", "DATA_BLOCKED", {}),
    }
    evidence_ids: dict[str, str] = {}
    for name, (evidence_id, action, versions) in evidence_specs.items():
        decision_run = decision_runs[name]
        evidence = DecisionEvidence(
            id=evidence_id,
            decision_run_id=decision_run.id,
            strategy_snapshot_id=snapshot.id,
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            trade_date=decision_run.trade_date,
            decision_at=decision_run.decision_at,
            data_cutoff_at=decision_run.data_cutoff_at,
            execution_at=decision_run.execution_at,
            action=action,
            min_lot_size=100,
            target_quantity=100.0,
            content_hash=(name[0] * 64),
            versions_json=json.dumps(versions),
        )
        db_session.add(evidence)
        evidence_ids[name] = evidence.id
    db_session.flush()

    db_session.add_all([
        DecisionOrderPlanRecord(
            order_plan_id="api-plan-entry",
            decision_run_id=decision_runs["entry"].id,
            evidence_id=evidence_ids["entry"],
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            action="BUY",
            signal_date=run_dates["entry"],
            execution_date=run_dates["entry"],
            target_quantity=300.0,
            direction="BUY",
            intended_price=9.99,
        ),
        DecisionOrderPlanRecord(
            order_plan_id="api-plan-exit",
            decision_run_id=decision_runs["exit"].id,
            evidence_id=evidence_ids["exit"],
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            action="SELL",
            signal_date=run_dates["exit"],
            execution_date=run_dates["exit"],
            target_quantity=100.0,
            direction="SELL",
            intended_price=10.45,
        ),
    ])
    db_session.flush()

    backtest_run = BacktestRun(
        portfolio_id=portfolio.id,
        run_name="API exact linkage run",
        symbols_json=json.dumps([symbol.id]),
        rule_config_json="{}",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 12),
        initial_capital=100_000.0,
        status="completed",
        strategy_snapshot_id=snapshot.id,
        decision_run_ids_json=json.dumps([run.id for run in decision_runs.values()]),
    )
    db_session.add(backtest_run)
    db_session.flush()
    db_session.add(
        BacktestTrade(
            run_id=backtest_run.id,
            symbol_id=symbol.id,
            entry_date=run_dates["entry"],
            entry_price=10.0,
            quantity=100.0,
            entry_cost=0.0,
            exit_date=run_dates["exit"],
            exit_price=10.5,
            exit_cost=0.0,
            decision_evidence_id=evidence_ids["entry"],
            exit_evidence_id=evidence_ids["exit"],
        )
    )
    db_session.commit()
    return backtest_run, evidence_ids


def test_trade_ledger_exposes_decision_runs_for_each_exact_evidence_leg(
    db_session,
):
    """The ledger must not infer a leg's run from a matching symbol/date."""
    backtest_run, _ = _seed_backtest_evidence(db_session)

    assert any(
        route.path == "/backtest/runs/{run_id}/trades" and "GET" in route.methods
        for route in router.routes
    )
    response = list_backtest_trades(
        backtest_run.id,
        page=1,
        page_size=20,
        limit=None,
        offset=0,
        action=None,
        symbol_id=None,
        start_date=None,
        end_date=None,
        execution_status=None,
        sort_by="signal_at",
        sort_dir="desc",
        db=db_session,
    )

    item = response["items"][0]
    assert item["decision_evidence_id"] == "api-evidence-entry-exact"
    assert item["entry_decision_run_id"] == "api-linkage-entry"
    assert item["exit_evidence_id"] == "api-evidence-exit-exact"
    assert item["exit_decision_run_id"] == "api-linkage-exit"
    assert item["entry_decision_run_id"] != "api-linkage-distractor"
    assert item["entry_order_plan_status"] == "PARTIAL_FILL_PENDING"
    assert item["entry_requested_quantity"] == 300.0
    assert item["entry_filled_quantity"] == 100.0
    assert item["entry_remaining_quantity"] == 200.0
    assert item["entry_unfilled_reason"] == "VOLUME_LIMIT"
    assert item["intended_entry_price"] == 9.99
    assert item["intended_exit_price"] == 10.45
    assert item["exit_order_plan_status"] == "FILLED"
    assert item["exit_requested_quantity"] == 100.0
    assert item["exit_filled_quantity"] == 100.0
    assert item["exit_remaining_quantity"] == 0.0


def test_rejected_evidence_filter_includes_execution_rejected_buy_and_sell(
    db_session,
):
    """The rejection tab includes matched BUY/SELL plans rejected by execution."""
    backtest_run, evidence_ids = _seed_backtest_evidence(db_session)

    assert any(
        route.path == "/backtest/runs/{run_id}/evidence" and "GET" in route.methods
        for route in router.routes
    )
    response = list_backtest_evidence(
        backtest_run.id,
        action="REJECTED,DATA_BLOCKED",
        page=1,
        page_size=20,
        limit=None,
        offset=0,
        db=db_session,
    )

    returned_ids = {item["id"] for item in response["items"]}
    assert response["total"] == 4
    assert {
        evidence_ids["direct_rejected"],
        evidence_ids["data_blocked"],
        evidence_ids["rejected_buy"],
        evidence_ids["rejected_sell"],
    } <= returned_ids
    assert evidence_ids["filled_buy"] not in returned_ids

    detail = _backtest_decision_context(db_session, backtest_run)
    assert detail["rejected_count"] == 4


def test_positions_projection_is_a_daily_holding_ledger(db_session):
    """The positions tab aggregates persisted lots into daily holding changes."""
    backtest_run, evidence_ids = _seed_backtest_evidence(db_session)
    symbol_id = json.loads(backtest_run.symbols_json)[0]
    backtest_run.end_date = date(2026, 8, 7)
    backtest_run.equity_curve_json = json.dumps([
        {"date": "2026-08-03", "equity": 1_000.0},
        {"date": "2026-08-04", "equity": 3_000.0},
        {"date": "2026-08-05", "equity": 1_500.0},
        {"date": "2026-08-06", "equity": 1_000.0},
        {"date": "2026-08-07", "equity": 1_000.0},
    ])
    db_session.query(BacktestTrade).filter(
        BacktestTrade.run_id == backtest_run.id
    ).delete()
    db_session.add_all([
        # The two lots prove that the endpoint is a day/symbol projection,
        # not a copy of the transaction ledger.
        BacktestTrade(
            run_id=backtest_run.id,
            symbol_id=symbol_id,
            entry_date=date(2026, 8, 3),
            entry_price=10.0,
            quantity=100.0,
            entry_cost=0.0,
            exit_date=date(2026, 8, 5),
            exit_price=11.0,
            exit_cost=0.0,
            decision_evidence_id=evidence_ids["entry"],
            exit_evidence_id=evidence_ids["exit"],
        ),
        BacktestTrade(
            run_id=backtest_run.id,
            symbol_id=symbol_id,
            entry_date=date(2026, 8, 4),
            entry_price=12.0,
            quantity=50.0,
            entry_cost=0.0,
            exit_date=date(2026, 8, 6),
            exit_price=10.0,
            exit_cost=0.0,
            decision_evidence_id=evidence_ids["filled_buy"],
            exit_evidence_id=evidence_ids["rejected_sell"],
        ),
    ])
    db_session.add_all([
        DailyBar(
            symbol_id=symbol_id,
            trade_date=trade_date,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=10000,
            amount=close * 10000,
        )
        for trade_date, close in (
            (date(2026, 8, 3), 10.0),
            (date(2026, 8, 4), 12.0),
            (date(2026, 8, 5), 11.0),
            (date(2026, 8, 6), 10.0),
            (date(2026, 8, 7), 10.0),
        )
    ])
    db_session.commit()

    response = list_backtest_positions(
        backtest_run.id,
        page=1,
        page_size=20,
        as_of_date=date(2026, 8, 7),
        status=None,
        db=db_session,
    )

    assert response["total"] == 4
    assert response["as_of_date"] == "2026-08-07"
    assert response["ledger_mode"] == "LEGACY_TRADE_APPROXIMATION"
    rows_by_date = {item["trade_date"]: item for item in response["items"]}
    assert rows_by_date["2026-08-03"] == {
        "run_id": backtest_run.id,
        "symbol_id": symbol_id,
        "trade_date": "2026-08-03",
        "opening_quantity": 0.0,
        "buy_quantity": 100.0,
        "sell_quantity": 0.0,
        "closing_quantity": 100.0,
        "status": "OPEN",
        "as_of_date": "2026-08-07",
        "mark_price": 10.0,
        "market_value": 1000.0,
        "portfolio_equity": 1000.0,
        "weight": 1.0,
        "buy_evidence_ids": [evidence_ids["entry"]],
        "sell_evidence_ids": [],
    }
    assert rows_by_date["2026-08-04"] == {
        "run_id": backtest_run.id,
        "symbol_id": symbol_id,
        "trade_date": "2026-08-04",
        "opening_quantity": 100.0,
        "buy_quantity": 50.0,
        "sell_quantity": 0.0,
        "closing_quantity": 150.0,
        "status": "OPEN",
        "as_of_date": "2026-08-07",
        "mark_price": 12.0,
        "market_value": 1800.0,
        "portfolio_equity": 3000.0,
        "weight": 0.6,
        "buy_evidence_ids": [evidence_ids["filled_buy"]],
        "sell_evidence_ids": [],
    }
    assert rows_by_date["2026-08-05"]["opening_quantity"] == 150.0
    assert rows_by_date["2026-08-05"]["buy_quantity"] == 0.0
    assert rows_by_date["2026-08-05"]["sell_quantity"] == 100.0
    assert rows_by_date["2026-08-05"]["closing_quantity"] == 50.0
    assert rows_by_date["2026-08-05"]["sell_evidence_ids"] == [evidence_ids["exit"]]
    assert rows_by_date["2026-08-06"]["status"] == "CLOSED"
    assert rows_by_date["2026-08-06"]["closing_quantity"] == 0.0
    assert rows_by_date["2026-08-06"]["weight"] == 0.0
    assert rows_by_date["2026-08-06"]["sell_evidence_ids"] == [evidence_ids["rejected_sell"]]

    closed = list_backtest_positions(
        backtest_run.id,
        page=1,
        page_size=20,
        as_of_date=date(2026, 8, 7),
        status="CLOSED",
        db=db_session,
    )
    assert closed["total"] == 1
    assert closed["items"][0]["trade_date"] == "2026-08-06"


def test_daily_holding_ledger_does_not_fill_a_missing_close_from_prior_day(db_session):
    """A missing same-day close preserves the position but not its valuation."""
    backtest_run, _ = _seed_backtest_evidence(db_session)
    symbol_id = json.loads(backtest_run.symbols_json)[0]
    backtest_run.end_date = date(2026, 8, 4)
    backtest_run.equity_curve_json = json.dumps([
        {"date": "2026-08-03", "equity": 1_000.0},
        {"date": "2026-08-04", "equity": 1_000.0},
    ])
    db_session.add(DailyBar(
        symbol_id=symbol_id,
        trade_date=date(2026, 8, 3),
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        volume=10_000,
        amount=100_000,
    ))
    db_session.commit()

    response = list_backtest_positions(
        backtest_run.id,
        page=1,
        page_size=20,
        as_of_date=date(2026, 8, 4),
        status=None,
        db=db_session,
    )

    assert response["total"] == 2
    rows_by_date = {item["trade_date"]: item for item in response["items"]}
    missing_price_row = rows_by_date["2026-08-04"]
    assert missing_price_row["opening_quantity"] == 100.0
    assert missing_price_row["closing_quantity"] == 100.0
    assert missing_price_row["mark_price"] is None
    assert missing_price_row["market_value"] is None
    assert missing_price_row["weight"] is None


def test_empty_run_uses_an_exact_empty_execution_ledger(db_session):
    """No fills is exact zero execution history, not a legacy approximation."""
    backtest_run, _ = _seed_backtest_evidence(db_session)
    db_session.query(BacktestTrade).filter(
        BacktestTrade.run_id == backtest_run.id
    ).delete()
    db_session.commit()

    response = list_backtest_positions(
        backtest_run.id,
        page=1,
        page_size=20,
        as_of_date=backtest_run.end_date,
        status=None,
        db=db_session,
    )

    assert response["ledger_mode"] == "EXECUTION_EVENTS"
    assert response["items"] == []
