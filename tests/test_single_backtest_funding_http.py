"""Single-symbol funding must reach the engine through the public HTTP contract.

All prices, scores and dated trading restrictions live in pytest's disposable
SQLite database. The app has no production lifespan and no internal mocks.
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



def _funding_scenario(db, *, flat_prices=False, base_price=10, symbol_code="600000", market="SH", asset_type="stock",
                      missing_history_day=None, suspended_on_entry=False, locked_limit_up=False, extra_symbol_codes=(),
                      prices=None, actions=None):
    portfolio = Portfolio(
        name="isolated-single-funding", account_type="simulated", total_capital=200_000,
        investable_ratio=0.95, cash_reserve_ratio=0.05,
    )
    symbols = [Symbol(symbol=code, name="隔离资金测试", market=market, asset_type=asset_type, is_st=0)
               for code in (symbol_code, *extra_symbol_codes)]
    db.add_all([portfolio, *symbols])
    db.flush()
    days = [date(2025, 9, 1) + timedelta(days=i) for i in range(len(prices) if prices else 3)]
    for i, day in enumerate(days):
        price = prices[i] if prices else (base_price + 0.5 if i == 2 and not flat_prices else base_price)
        action = actions[i] if actions else ("open" if i == 0 else "exit")
        if locked_limit_up and i == 1:
            price = 11
        db.add(MarketCalendar(trade_date=day, is_trading_day=1))
        for symbol in symbols:
            db.add(DailyBar(
                symbol_id=symbol.id, trade_date=day, open=price, close=price,
                high=price if locked_limit_up and i == 1 else price + 0.1,
                low=price if locked_limit_up and i == 1 else price - 0.1, volume=100_000, amount=1_000_000,
                created_at=datetime(2025, 8, 1),
            ))
            db.add(Score(
                symbol_id=symbol.id, trade_date=day, quality_score=90 if action == "open" else 20,
                quality_grade="A", timing_score=80, stage="growth",
                action=action, priority_score=80,
                model_alpha_score=90 if action == "open" else 20, weight_mode="manual",
                factor_data_cutoff_at=datetime(2025, 8, 1), published_at=datetime(2025, 8, 1),
            ))
    db.commit()
    limits = "LIMITED,11,9,0.01" if locked_limit_up else "NO_LIMIT,,,0.01"
    history = SecurityStatusFileRequest(
        csv_text="symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,"
                 "price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n"
                 + "".join(f"{symbol.symbol},{day},0,{int(suspended_on_entry and i == 1)},0,1,1999-11-10,,{limits}\n"
                           for symbol in symbols for i, day in enumerate(days) if i != missing_history_day),
        source_name="provider_archive", source_reference="archive://funding-test/daily/20250903",
        source_version="release-20250903", source_updated_at=f"{days[-1]}T18:00:00+08:00",
    )
    preview = preview_import(db, history)
    assert preview["can_import"], preview
    import_file(db, history, preview_sha256=preview["content_sha256"])
    db.commit()
    return {
        "portfolio_id": portfolio.id, "symbol_ids": [symbol.id for symbol in symbols],
        "start_date": str(days[0]), "end_date": str(days[-1]),
        "score_weight_mode": "manual", "initial_capital": 100_000,
        "rule_config": {
            "buy_conditions": {"quality_score_min": 80},
            "sell_conditions": {"score_actions": ["exit"]},
            "position_config": {"type": "fixed_pct", "value": 0.5},
            "execution_config": {"entry_timing": "next_open", "exit_timing": "next_open"},
        },
        "cost_config": {
            "commission_rate": 0, "min_commission": 0, "stamp_tax_rate": 0,
            "transfer_fee_rate": 0, "slippage_rate": 0,
            "slippage_buy_bps": 0, "slippage_sell_bps": 0,
        },
    }


@pytest.fixture
def funding_client(db_session):
    from app.main import http_exception_handler, validation_error_handler

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def test_single_http_initial_capital_is_saved_and_used_for_real_quantity_and_equity(db_session):
    payload = _funding_scenario(db_session)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        submitted = client.post("/api/v1/backtest/run", json=payload)
        assert submitted.status_code == 200, submitted.text
        run_id = submitted.json()["id"]
        read = client.get(f"/api/v1/backtest/runs/{run_id}")
        assert read.status_code == 200, read.text
        trades = client.get(f"/api/v1/backtest/runs/{run_id}/trades", params={"page": 1, "page_size": 20})
        assert trades.status_code == 200, trades.text

    # A 100,000 budget with 50% exposure buys 5,000 shares at 10.00. Selling
    # at 10.50 makes 2,500. These literals are a worked example, not a second
    # copy of the implementation. The portfolio's 200,000 must not be used.
    assert submitted.json()["initial_capital"] == 100_000
    result = read.json()
    assert result["initial_capital"] == 100_000
    assert result["status"] == "completed"
    assert trades.json()["total"] == 1
    trade = trades.json()["items"][0]
    assert trade["quantity"] == 5_000
    assert trade["entry_price"] == 10
    assert trade["exit_price"] == 10.5
    assert trade["pnl"] == 2_500
    assert result["total_return"] == 2_500
    equity = json.loads(result["equity_curve_json"])
    assert equity[0]["equity"] == 100_000
    assert equity[-1]["equity"] == 102_500


def test_single_http_full_position_keeps_commission_and_slippage_in_cash_budget(db_session):
    payload = _funding_scenario(db_session, flat_prices=True)
    payload["rule_config"]["position_config"]["value"] = 1
    payload["cost_config"].update(commission_rate=0.0003, min_commission=5, slippage_rate=0.001)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        submitted = client.post("/api/v1/backtest/run", json=payload)
        assert submitted.status_code == 200, submitted.text
        run_id = submitted.json()["id"]
        read = client.get(f"/api/v1/backtest/runs/{run_id}")
        assert read.status_code == 200, read.text
        trades = client.get(f"/api/v1/backtest/runs/{run_id}/trades", params={"page": 1, "page_size": 20})
        assert trades.status_code == 200, trades.text

    # 10,000 shares cost 100,130 including 30 commission and 100 slippage.
    # One fewer legal 100-share lot costs 99,000 + 29.70 + 99 = 99,128.70.
    # The order must shrink to 9,900 shares instead of disappearing entirely.
    assert trades.json()["total"] == 1
    trade = trades.json()["items"][0]
    assert trade["quantity"] == 9_900
    assert trade["entry_price"] == 10
    assert trade["entry_cost"] == 128.70
    assert trade["entry_price"] * trade["quantity"] + trade["entry_cost"] == 99_128.70
    equity = json.loads(read.json()["equity_curve_json"])
    assert equity[1]["equity"] == 99_871.30


def test_single_http_omitted_initial_capital_preserves_portfolio_default(db_session, funding_client):
    payload = _funding_scenario(db_session)
    del payload["initial_capital"]
    submitted = funding_client.post("/api/v1/backtest/run", json=payload)
    assert submitted.status_code == 200, submitted.text
    run_id = submitted.json()["id"]
    read = funding_client.get(f"/api/v1/backtest/runs/{run_id}")
    trades = funding_client.get(f"/api/v1/backtest/runs/{run_id}/trades")
    assert read.status_code == trades.status_code == 200
    assert submitted.json()["initial_capital"] == 200_000
    assert read.json()["initial_capital"] == 200_000
    assert trades.json()["items"][0]["quantity"] == 10_000
    assert read.json()["total_return"] == 5_000


@pytest.mark.parametrize("invalid_capital", [0, -1, float("nan"), float("inf")], ids=["zero", "negative", "nan", "infinite"])
def test_single_http_rejects_invalid_initial_capital_without_creating_run(db_session, funding_client, invalid_capital):
    payload = _funding_scenario(db_session)
    before = funding_client.get("/api/v1/backtest/runs", params={"portfolio_id": payload["portfolio_id"]})
    assert before.status_code == 200
    payload["initial_capital"] = invalid_capital
    # Raw JSON exercises the HTTP validator for non-finite values; httpx's
    # json= encoder rejects them before the request reaches the API.
    submitted = funding_client.post(
        "/api/v1/backtest/run", content=json.dumps(payload), headers={"Content-Type": "application/json"},
    )
    assert submitted.status_code == 422, submitted.text
    after = funding_client.get("/api/v1/backtest/runs", params={"portfolio_id": payload["portfolio_id"]})
    assert after.status_code == 200
    assert after.json() == before.json() == []


@pytest.mark.parametrize("capital,minimum_commission", [(61, 5), (56, 0)], ids=["exact-total-with-fee", "exact-one-lot-no-fee"])
def test_single_http_exact_cent_budget_buys_one_etf_lot_without_float_rejection(db_session, funding_client, capital, minimum_commission):
    payload = _funding_scenario(
        db_session, flat_prices=True, base_price=0.56, symbol_code="510300", market="sh", asset_type="etf",
    )
    payload["initial_capital"] = capital
    payload["rule_config"]["position_config"]["value"] = 1
    payload["cost_config"].update(commission_rate=0, min_commission=minimum_commission, slippage_rate=0)
    submitted = funding_client.post("/api/v1/backtest/run", json=payload)
    assert submitted.status_code == 200, submitted.text
    run_id = submitted.json()["id"]
    trades = funding_client.get(f"/api/v1/backtest/runs/{run_id}/trades")
    assert trades.status_code == 200
    assert trades.json()["total"] == 1
    trade = trades.json()["items"][0]
    # The worked examples are exactly 61.00 with a 5.00 fee and 56.00 with
    # none. Floats can overstate the total or understate the one-lot boundary.
    assert trade["quantity"] == 100
    assert trade["entry_cost"] == minimum_commission


@pytest.mark.parametrize(
    "capital,position_pct,commission,minimum,slippage,expected_quantity,expected_entry_cost",
    [
        (100_000, 1, 0, 0, 0.001, 9_900, 99),
        (100_000, 1, 0, 5, 0, 9_900, 5),
        (100_000, 1, 0, 0, 0, 10_000, 0),
        (1_006, 1, 0, 5, 0.001, 100, 6),
        (1_005.99, 1, 0, 5, 0.001, 0, 0),
        (100_000, 0.5, 0.0003, 5, 0.001, 5_000, 65),
    ],
    ids=["slippage-only", "minimum-only", "free", "one-lot-exact", "one-cent-short", "small-position-already-funded"],
)
def test_single_http_affordable_lots_keep_fee_and_position_semantics(
    db_session, funding_client, capital, position_pct, commission, minimum, slippage,
    expected_quantity, expected_entry_cost,
):
    payload = _funding_scenario(db_session, flat_prices=True)
    payload["initial_capital"] = capital
    payload["rule_config"]["position_config"]["value"] = position_pct
    payload["cost_config"].update(commission_rate=commission, min_commission=minimum, slippage_rate=slippage)
    submitted = funding_client.post("/api/v1/backtest/run", json=payload)
    assert submitted.status_code == 200, submitted.text
    run_id = submitted.json()["id"]
    read = funding_client.get(f"/api/v1/backtest/runs/{run_id}")
    trades = funding_client.get(f"/api/v1/backtest/runs/{run_id}/trades")
    assert read.status_code == trades.status_code == 200
    if expected_quantity == 0:
        assert trades.json()["total"] == 0
        assert all(point["equity"] == capital for point in json.loads(read.json()["equity_curve_json"]))
    else:
        assert trades.json()["total"] == 1
        trade = trades.json()["items"][0]
        assert trade["quantity"] == expected_quantity
        assert trade["quantity"] % 100 == 0
        assert trade["entry_cost"] == expected_entry_cost
        assert trade["entry_price"] * trade["quantity"] + trade["entry_cost"] <= capital


@pytest.mark.parametrize("timing", ["next_open", "signal_close"])
@pytest.mark.parametrize("symbol_code,market,asset_type", [("600000", "sh", "stock"), ("510300", "sh", "etf")])
def test_single_http_both_entry_paths_keep_legal_lots_for_lowercase_cn_market(
    db_session, funding_client, timing, symbol_code, market, asset_type,
):
    payload = _funding_scenario(
        db_session, flat_prices=True, symbol_code=symbol_code, market=market, asset_type=asset_type,
    )
    payload["rule_config"]["position_config"]["value"] = 1
    payload["rule_config"]["execution_config"]["entry_timing"] = timing
    payload["cost_config"].update(commission_rate=0.0003, min_commission=5, slippage_rate=0.001)
    submitted = funding_client.post("/api/v1/backtest/run", json=payload)
    assert submitted.status_code == 200, submitted.text
    trades = funding_client.get(f"/api/v1/backtest/runs/{submitted.json()['id']}/trades")
    assert trades.status_code == 200
    assert trades.json()["total"] == 1
    trade = trades.json()["items"][0]
    assert trade["quantity"] == 9_900
    assert trade["entry_cost"] == 128.70
    assert trade["entry_date"] == ("2025-09-02" if timing == "next_open" else "2025-09-01")


@pytest.mark.parametrize("restriction", ["missing-history", "suspended", "limit-up"])
def test_single_http_cash_sizing_does_not_bypass_historical_execution_guards(db_session, funding_client, restriction):
    payload = _funding_scenario(
        db_session, flat_prices=True,
        missing_history_day=1 if restriction == "missing-history" else None,
        suspended_on_entry=restriction == "suspended", locked_limit_up=restriction == "limit-up",
    )
    payload["rule_config"]["position_config"]["value"] = 1
    payload["cost_config"].update(commission_rate=0.0003, min_commission=5, slippage_rate=0.001)
    submitted = funding_client.post("/api/v1/backtest/run", json=payload)
    if restriction == "missing-history":
        assert submitted.status_code == 409, submitted.text
        assert submitted.json()["error_code"] == "STATUS_DATA_UNAVAILABLE"
        history = funding_client.get("/api/v1/backtest/runs", params={"portfolio_id": payload["portfolio_id"]})
        assert history.status_code == 200
        assert history.json() == []
    else:
        assert submitted.status_code == 200, submitted.text
        trades = funding_client.get(f"/api/v1/backtest/runs/{submitted.json()['id']}/trades")
        assert trades.status_code == 200
        assert trades.json()["total"] == 0


def test_http_repeated_exact_cash_deductions_do_not_drop_the_last_affordable_lot(db_session, funding_client):
    payload = _funding_scenario(
        db_session, flat_prices=True, base_price=0.55, symbol_code="510300",
        extra_symbol_codes=("510050", "510500"), market="sh", asset_type="etf",
    )
    payload["initial_capital"] = 180.06
    payload["rule_config"]["position_config"].update(type="fixed_amount", value=55, max_positions=3)
    payload["cost_config"].update(commission_rate=0.0003, min_commission=5, slippage_rate=0.0003)
    submitted = funding_client.post("/api/v1/backtest/run", json=payload)
    assert submitted.status_code == 200, submitted.text
    run_id = submitted.json()["id"]
    trades = funding_client.get(f"/api/v1/backtest/runs/{run_id}/trades")
    assert trades.status_code == 200
    # Each 100-share lot costs 55.00 + 5.02 = 60.02. Three fit exactly
    # inside 180.06; a binary subtraction must not erase the third fill.
    assert trades.json()["total"] == 3
    assert {trade["symbol_id"] for trade in trades.json()["items"]} == set(payload["symbol_ids"])
    for trade in trades.json()["items"]:
        assert trade["quantity"] == 100
        assert trade["entry_cost"] == 5.02


def test_single_http_sale_proceeds_fund_the_next_exact_budget_purchase(db_session, funding_client):
    payload = _funding_scenario(
        db_session, symbol_code="510300", market="sh", asset_type="etf",
        prices=[0.55, 0.55, 0.55, 0.45], actions=["open", "exit", "open", "hold"],
    )
    payload["initial_capital"] = 60.06
    payload["rule_config"]["position_config"].update(type="fixed_amount", value=55, max_positions=1)
    payload["cost_config"].update(commission_rate=0, min_commission=5, slippage_rate=0.0004)
    submitted = funding_client.post("/api/v1/backtest/run", json=payload)
    assert submitted.status_code == 200, submitted.text
    run_id = submitted.json()["id"]
    trades = funding_client.get(f"/api/v1/backtest/runs/{run_id}/trades")
    assert trades.status_code == 200
    # First buy: 60.06 - 60.02 = 0.04 cash. The sale adds 55 - 5.02 =
    # 49.98, leaving exactly 50.02, enough for 100 at 0.45 plus 5.02 fees.
    assert trades.json()["total"] == 2
    ordered = sorted(trades.json()["items"], key=lambda item: item["entry_date"])
    assert ordered[0]["entry_date"] == "2025-09-02"
    assert ordered[0]["exit_date"] == "2025-09-03"
    assert ordered[0]["quantity"] == 100
    assert ordered[0]["exit_cost"] == 5.02
    assert ordered[1]["entry_date"] == "2025-09-04"
    assert ordered[1]["quantity"] == 100
    assert ordered[1]["entry_price"] == 0.45
    assert ordered[1]["entry_cost"] == 5.02
