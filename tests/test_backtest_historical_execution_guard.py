"""Backtests must use exact dated trading restrictions, never today's flags."""
from datetime import date, datetime, timedelta
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.routes.backtest import list_backtest_trades
from app.models.backtest import BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.bfg_precheck import BacktestPrecheckRequest
from app.schemas.security_status import SecurityStatusFileRequest
from app.services.backtest import run_backtest
from app.services.bfg_precheck_service import run_backtest_precheck
from app.services.security_status.local_import import import_file, preview_import

pytestmark = pytest.mark.whitebox


def scenario(db, *, snapshot=False, current_st=0):
    portfolio = Portfolio(name="history-guard", account_type="simulated", total_capital=200_000,
                          investable_ratio=0.95, cash_reserve_ratio=0.05)
    symbol = Symbol(symbol="600000", name="ST当前名称" if current_st else "当前普通名称",
                    market="SH", asset_type="stock", is_st=current_st)
    db.add_all([portfolio, symbol])
    db.flush()
    days = [date(2025, 9, 1) + timedelta(days=i) for i in range(3)]
    for i, day in enumerate(days):
        db.add(MarketCalendar(trade_date=day, is_trading_day=1))
        db.add(DailyBar(symbol_id=symbol.id, trade_date=day, open=10, close=10,
                        high=10.1, low=9.9, volume=100_000, amount=1_000_000,
                        created_at=datetime(2025, 8, 1)))
        db.add(Score(symbol_id=symbol.id, trade_date=day, quality_score=90 if i == 0 else 20,
                     quality_grade="A", timing_score=80, stage="growth",
                     action="open" if i == 0 else "exit", priority_score=80,
                     model_alpha_score=90 if i == 0 else 20,
                     weight_mode="ridge" if snapshot else "manual",
                     factor_model_run_id="historical-model" if snapshot else None,
                     factor_data_cutoff_at=datetime(2025, 8, 1),
                     published_at=datetime(2025, 8, 1)))
    kwargs = dict(db=db, portfolio_id=portfolio.id, symbol_ids=[symbol.id],
                  start_date=days[0], end_date=days[-1], score_weight_mode="manual",
                  rule_config={"buy_conditions": {"quality_score_min": 80},
                               "sell_conditions": {"score_actions": ["exit"]},
                               "position_config": {"type": "fixed_pct", "value": 0.05},
                               "execution_config": {"entry_timing": "next_open", "exit_timing": "next_open"}},
                  cost_config={"commission_rate": 0, "min_commission": 0, "stamp_tax_rate": 0,
                               "transfer_fee_rate": 0, "slippage_rate": 0,
                               "slippage_buy_bps": 0, "slippage_sell_bps": 0})
    if snapshot:
        frozen = StrategyExecutionSnapshot(
            id="history-guard-snapshot", snapshot_no=1, portfolio_id=portfolio.id,
            factor_model_run_id="historical-model", decision_clock_json="{}",
            member_snapshot_json=json.dumps([{"symbol_id": symbol.id, "member_id": 1}]),
            snapshot_type="save_and_apply", snapshot_hash="history-guard-hash",
            gate_policy_version="production-v1.0.0", effective_from=datetime(2025, 8, 1),
            versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}))
        db.add(frozen)
        kwargs.pop("score_weight_mode")
        kwargs["strategy_snapshot_id"] = frozen.id
    db.commit()
    return kwargs, symbol, days


def import_history(db, days, *, omit=None, limits=True, flags=None, prices=None):
    rows = []
    for i, day in enumerate(days):
        if day == omit:
            continue
        st, suspended = (flags or {}).get(i, (0, 0))
        bounds = (prices or {}).get(i, "LIMITED,11,9,0.01") if limits else ",,,"
        rows.append(f"600000,{day},{st},{suspended},0,1,1999-11-10,,{bounds}\n")
    payload = SecurityStatusFileRequest(
        csv_text="symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,"
                 "price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n" + "".join(rows),
        source_name="provider_archive", source_reference="archive://history/daily/20250903",
        source_version="release-20250903", source_updated_at="2025-09-03T18:00:00+08:00")
    preview = preview_import(db, payload)
    assert preview["can_import"], preview
    import_file(db, payload, preview_sha256=preview["content_sha256"])
    db.commit()


@pytest.mark.parametrize("snapshot", [False, True])
def test_missing_historical_day_blocks_before_creating_any_run(db_session, snapshot):
    kwargs, symbol, days = scenario(db_session, snapshot=snapshot)
    import_history(db_session, days, omit=days[1])
    # Current metadata is complete but must not fill the missing execution day.
    assert symbol.is_st == 0 and symbol.is_active == 1
    with pytest.raises(ValueError, match="历史.*资料"):
        run_backtest(**kwargs)
    assert db_session.scalar(select(func.count()).select_from(BacktestRun)) == 0
    assert db_session.scalar(select(func.count()).select_from(DecisionRun)) == 0
    assert db_session.scalar(select(func.count()).select_from(BacktestTrade)) == 0


def test_precheck_blocks_missing_daily_limits_even_with_complete_status(db_session):
    kwargs, symbol, days = scenario(db_session)
    import_history(db_session, days, limits=False)
    result = run_backtest_precheck(db_session, BacktestPrecheckRequest(
        portfolio_id=kwargs["portfolio_id"], symbol_ids=[symbol.id],
        start_date=days[0], end_date=days[-1], minimum_trade_days=1))
    assert "PRICE_LIMIT_DATA_UNAVAILABLE" in {item.code for item in result.blocking_reasons}


def trades(db, run):
    return list_backtest_trades(run.id, page=1, page_size=200, limit=None, offset=0,
        action=None, symbol_id=None, start_date=None, end_date=None, execution_status=None,
        sort_by="execution_at", sort_dir="asc", db=db)["items"]


def test_snapshot_uses_original_limits_even_if_stock_is_st_today(db_session):
    kwargs, symbol, days = scenario(db_session, snapshot=True, current_st=1)
    import_history(db_session, days)
    bar = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date == days[1]))
    bar.open = bar.high = bar.low = bar.close = 10.8
    db_session.commit()
    run = run_backtest(**kwargs)
    rows = trades(db_session, run)
    assert len(rows) == 1, [(e.action, e.rejection_reason, e.rejection_detail) for e in db_session.scalars(select(DecisionEvidence))]
    assert rows[0]["entry_price"] == 10.8


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("restriction", ["st", "suspended", "limit_up"])
def test_execution_day_restriction_prevents_a_new_position(db_session, snapshot, restriction):
    kwargs, symbol, days = scenario(db_session, snapshot=snapshot)
    flags = {1: (1, 0)} if restriction == "st" else {1: (0, 1)} if restriction == "suspended" else None
    import_history(db_session, days, flags=flags)
    if restriction == "limit_up":
        bar = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date == days[1]))
        bar.open = bar.high = bar.low = bar.close = 11
        db_session.commit()
    run = run_backtest(**kwargs)
    assert trades(db_session, run) == []


def test_single_symbol_http_run_returns_actionable_history_error(db_session):
    from app.api.routes.backtest import router
    from app.db.session import get_db
    kwargs, symbol, days = scenario(db_session)
    import_history(db_session, days, limits=False)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    from app.main import http_exception_handler
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/v1/backtest/run", json={
            "portfolio_id": kwargs["portfolio_id"], "symbol_ids": [symbol.id],
            "start_date": str(days[0]), "end_date": str(days[-1]),
            "score_weight_mode": "manual", "rule_config": kwargs["rule_config"]})
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "PRICE_LIMIT_DATA_UNAVAILABLE"
    assert response.json()["extras"]["missing_sample"][0]["trade_date"] == str(days[0])
    assert db_session.scalar(select(func.count()).select_from(BacktestRun)) == 0


def test_project_vectorbt_cannot_skip_missing_historical_limits(db_session):
    from app.services.vectorbt_backtest import VectorBTConfig, run_project_vectorbt_backtest
    kwargs, symbol, days = scenario(db_session)
    import_history(db_session, days, limits=False)
    with pytest.raises(ValueError, match="历史涨跌停资料"):
        run_project_vectorbt_backtest(db_session, symbol_ids=[symbol.id], start_date=days[0], end_date=days[-1],
            config=VectorBTConfig(signal_mode="ma_cross", fast_window=1, slow_window=2, stamp_tax_rate=0))


@pytest.mark.parametrize("snapshot", [False, True])
def test_explicit_no_limit_never_falls_back_to_ten_percent(db_session, snapshot):
    kwargs, symbol, days = scenario(db_session, snapshot=snapshot)
    import_history(db_session, days, prices={1: "NO_LIMIT,,,0.01", 2: "NO_LIMIT,,,0.01"})
    for bar in db_session.scalars(select(DailyBar).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date >= days[1])):
        bar.open = bar.high = bar.low = bar.close = 12
    db_session.commit()
    rows = trades(db_session, run_backtest(**kwargs))
    assert len(rows) == 1
    assert rows[0]["entry_price"] == 12


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("restriction", ["st", "suspended", "limit_down"])
def test_held_position_uses_historical_sell_restrictions(db_session, snapshot, restriction):
    kwargs, symbol, days = scenario(db_session, snapshot=snapshot)
    flags = {2: (1, 0)} if restriction == "st" else {2: (0, 1)} if restriction == "suspended" else None
    import_history(db_session, days, flags=flags)
    if restriction == "limit_down":
        bar = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date == days[2]))
        bar.open = bar.high = bar.low = bar.close = 9
        db_session.commit()
    rows = trades(db_session, run_backtest(**kwargs))
    assert len(rows) == 1
    assert rows[0]["entry_price"] == 10
    assert rows[0]["exit_price"] == (10 if restriction == "st" else None)


def test_turning_off_research_filters_cannot_allow_an_unknown_day(db_session):
    from app.schemas.backtest import BacktestFilterConfigDTO
    kwargs, symbol, days = scenario(db_session)
    fourth = days[-1] + timedelta(days=1)
    db_session.add(MarketCalendar(trade_date=fourth, is_trading_day=1))
    db_session.commit()
    import_history(db_session, days)
    # Three of four days are covered: the old 70% precheck threshold alone
    # would allow the unprovided fourth day when production_fidelity=False.
    result = run_backtest_precheck(db_session, BacktestPrecheckRequest(
        portfolio_id=kwargs["portfolio_id"], symbol_ids=[symbol.id], start_date=days[0],
        end_date=fourth, minimum_trade_days=1, filter_config=BacktestFilterConfigDTO(
            production_fidelity=False, filter_st=False, filter_suspended=False)))
    assert "STATUS_DATA_UNAVAILABLE" in {item.code for item in result.blocking_reasons}


def test_changed_original_limits_block_replay_instead_of_reusing_cached_evidence(db_session):
    from app.models.audit import DataGovernanceAuditEvent
    kwargs, _, days = scenario(db_session)
    import_history(db_session, days)
    first = run_backtest(**kwargs)
    assert first.status == "completed"
    audit = db_session.scalar(select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.business_key.like("%:execution:0")))
    audit.after_json = audit.after_json.replace('"upper_limit_price":11.0', '"upper_limit_price":12.0')
    db_session.commit()
    with pytest.raises(ValueError, match="历史涨跌停资料"):
        run_backtest(**kwargs)
    assert db_session.scalar(select(func.count()).select_from(BacktestRun)) == 1
