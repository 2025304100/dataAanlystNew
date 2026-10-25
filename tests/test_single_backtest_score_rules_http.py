"""Displayed single-symbol rules must describe the real HTTP execution behavior.

The route and engine use disposable SQLite records for prices, dated scores,
the trade calendar and historical restrictions. No application lifespan,
production database, downloads, training or internal mocks are involved.
"""
from datetime import date, datetime, timedelta
import json

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
import pytest

from app.api.routes.backtest import router
from app.db.session import get_db
from app.models.daily_bar import DailyBar
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.security_status import SecurityStatusFileRequest
from app.services.security_status.local_import import import_file, preview_import

import pytest

pytestmark = pytest.mark.whitebox



@pytest.fixture
def score_rule_client(db_session):
    from app.main import http_exception_handler, validation_error_handler

    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.add_exception_handler(HTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.dependency_overrides[get_db] = lambda: db_session
    with TestClient(application) as client:
        yield client


def _score_scenario(db, *, opens, closes=None, first_quality=50, first_timing=60,
                    first_action="open", later_actions=None, days=None):
    """Create input evidence; observations are made only through HTTP below."""
    portfolio = Portfolio(
        name="isolated-single-score-rules", account_type="simulated", total_capital=200_000,
        investable_ratio=0.95, cash_reserve_ratio=0.05,
    )
    symbol = Symbol(symbol="600000", name="隔离评分规则测试", market="SH", asset_type="stock", is_st=0)
    db.add_all([portfolio, symbol])
    db.flush()
    days = days or [date(2025, 9, 1) + timedelta(days=i) for i in range(len(opens))]
    closes = closes or opens
    later_actions = later_actions or {}
    calendar_day = days[0]
    while calendar_day <= days[-1]:
        db.add(MarketCalendar(trade_date=calendar_day, is_trading_day=int(calendar_day in days)))
        calendar_day += timedelta(days=1)
    for index, day in enumerate(days):
        action = first_action if index == 0 else later_actions.get(index, "hold")
        db.add(DailyBar(
            symbol_id=symbol.id, trade_date=day, open=opens[index], close=closes[index],
            high=max(opens[index], closes[index]) + 0.1, low=min(opens[index], closes[index]) - 0.1,
            volume=100_000, amount=1_000_000, created_at=datetime(2025, 1, 1),
        ))
        db.add(Score(
            symbol_id=symbol.id, trade_date=day,
            quality_score=first_quality if index == 0 else 50,
            quality_grade="A", timing_score=first_timing if index == 0 else 60,
            stage="growth", action=action, priority_score=80,
            model_alpha_score=first_quality if index == 0 else 50, weight_mode="manual",
            factor_data_cutoff_at=datetime(2025, 1, 1), published_at=datetime(2025, 1, 1),
        ))
    db.commit()
    history = SecurityStatusFileRequest(
        csv_text="symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,"
                 "price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n"
                 + "".join(f"600000,{day},0,0,0,1,1999-11-10,,NO_LIMIT,,,0.01\n" for day in days),
        source_name="provider_archive", source_reference="archive://single-score-rules/daily",
        source_version="isolated-test-release", source_updated_at=f"{days[-1]}T18:00:00+08:00",
    )
    preview = preview_import(db, history)
    assert preview["can_import"], preview
    import_file(db, history, preview_sha256=preview["content_sha256"])
    db.commit()
    return {
        "portfolio_id": portfolio.id, "symbol_ids": [symbol.id],
        "start_date": str(days[0]), "end_date": str(days[-1]),
        "initial_capital": 100_000, "score_weight_mode": "manual",
        "rule_config": {
            "buy_conditions": {
                "quality_score_min": 50, "timing_score_min": 60,
                "actions": ["buy", "open"], "stages": [],
            },
            "sell_conditions": {
                "take_profit_pct": 0.20, "stop_loss_pct": 0.08,
                "max_hold_days": 30, "score_actions": ["exit", "reduce"],
            },
            "position_config": {"type": "fixed_pct", "value": 1, "max_positions": 1},
            "execution_config": {
                "entry_timing": "next_open", "exit_timing": "next_open",
                "entry_price_field": "open", "exit_price_field": "open",
            },
        },
        "cost_config": {"commission_rate": 0, "min_commission": 0, "slippage_rate": 0, "stamp_tax_rate": 0},
    }


def _run_and_read(client, payload):
    submitted = client.post("/api/v1/backtest/run", json=payload)
    assert submitted.status_code == 200, submitted.text
    run_id = submitted.json()["id"]
    detail = client.get(f"/api/v1/backtest/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "completed"
    # The HTTP response must retain the exact documented rule, with no hidden
    # indicator preset substituted between submission and execution.
    assert json.loads(detail.json()["rule_config_json"]) == payload["rule_config"]
    trades = client.get(f"/api/v1/backtest/runs/{run_id}/trades", params={"page": 1, "page_size": 20})
    assert trades.status_code == 200, trades.text
    return detail.json(), trades.json()


@pytest.mark.parametrize(
    "quality,timing,action,expected_count",
    [(49, 60, "open", 0), (50, 59, "open", 0), (50, 60, "hold", 0),
     (50, 60, "open", 1), (50, 60, "buy", 1)],
    ids=["quality-below-minimum", "timing-below-minimum", "non-buy-action", "open-at-boundary", "buy-at-boundary"],
)
def test_single_score_rule_requires_all_buy_conditions_at_the_documented_boundaries(
    db_session, score_rule_client, quality, timing, action, expected_count,
):
    payload = _score_scenario(
        db_session, opens=[10, 10, 10], first_quality=quality, first_timing=timing, first_action=action,
    )
    _, trades = _run_and_read(score_rule_client, payload)
    assert trades["total"] == expected_count
    if expected_count:
        trade = trades["items"][0]
        assert trade["entry_date"] == "2025-09-02"
        assert trade["entry_price"] == 10
        assert trade["quantity"] == 10_000
        assert trade["exit_date"] is None


@pytest.mark.parametrize(
    "signal_price,execution_price,action,expected_reason,expected_pnl",
    [(12, 13, "hold", "take_profit", 30_000), (9.2, 9, "hold", "stop_loss", -10_000),
     (10, 10, "exit", "score_action", 0), (10, 10, "reduce", "score_action", 0)],
    ids=["twenty-percent-profit", "eight-percent-loss", "exit-action", "reduce-action-also-closes-entire-position"],
)
def test_single_score_rule_exits_the_entire_position_on_the_next_open(
    db_session, score_rule_client, signal_price, execution_price, action, expected_reason, expected_pnl,
):
    payload = _score_scenario(
        db_session, opens=[10, 10, signal_price, execution_price], later_actions={2: action},
    )
    _, trades = _run_and_read(score_rule_client, payload)
    assert trades["total"] == 1
    trade = trades["items"][0]
    assert trade["entry_date"] == "2025-09-02"
    assert trade["entry_price"] == 10
    assert trade["quantity"] == 10_000
    assert trade["exit_date"] == "2025-09-04"
    assert trade["exit_price"] == execution_price
    assert trade["exit_reason"] == expected_reason
    assert trade["hold_days"] == 2
    # A closed 10,000-share trade realizes the entire position's profit/loss.
    # reduce is a sell trigger in this rule; it does not mean a partial sale.
    assert trade["pnl"] == expected_pnl


def test_single_score_rule_counts_thirty_calendar_days_then_waits_for_next_trading_open(db_session, score_rule_client):
    calendar_days = [date(2025, 7, 1) + timedelta(days=i) for i in range(35)]
    trading_days = [day for day in calendar_days if day.weekday() < 5]
    payload = _score_scenario(db_session, days=trading_days, opens=[10] * len(trading_days))
    _, trades = _run_and_read(score_rule_client, payload)
    assert trades["total"] == 1
    trade = trades["items"][0]
    # July 2 entry + 30 calendar days is Friday August 1. The execution
    # waits across a weekend, so the full position closes Monday August 4.
    assert trade["entry_date"] == "2025-07-02"
    assert trade["exit_date"] == "2025-08-04"
    assert trade["exit_reason"] == "max_hold_days"
    assert trade["hold_days"] == 33


def test_single_score_rule_checks_the_signal_day_open_not_close_for_profit_exit(db_session, score_rule_client):
    payload = _score_scenario(db_session, opens=[10, 10, 12, 13], closes=[10, 10, 10, 13])
    _, trades = _run_and_read(score_rule_client, payload)
    assert trades["total"] == 1
    trade = trades["items"][0]
    # The documented rule checks Wednesday's 12 open against the 10 entry,
    # even though Wednesday closes at 10; it executes Thursday's 13 open.
    assert trade["entry_date"] == "2025-09-02"
    assert trade["entry_price"] == 10
    assert trade["exit_reason"] == "take_profit"
    assert trade["exit_date"] == "2025-09-04"
    assert trade["exit_price"] == 13
