"""Settings contracts through public APIs; all storage is isolated by db_session."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import akshare_apis, custom_indicators, discovery, scheduled_tasks, scoring_configs, signal_rules
from app.db.session import get_db


@pytest.fixture
def schedule_client(db_session):
    app = FastAPI()
    app.include_router(scheduled_tasks.router, prefix="/api/v1")
    app.include_router(akshare_apis.router, prefix="/api/v1")
    app.include_router(signal_rules.router, prefix="/api/v1")
    app.include_router(custom_indicators.router, prefix="/api/v1")
    app.include_router(discovery.router, prefix="/api/v1")
    app.include_router(scoring_configs.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def test_quarterly_grade_review_can_be_saved_and_updated(schedule_client):
    response = schedule_client.post("/api/v1/scheduled-tasks", json={
        "name": "Grade review", "task_type": "factor_grade_review",
        "frequency": "weekly", "time_of_day": "18:00", "weekdays": [4],
        "timezone": "Asia/Shanghai", "enabled": False,
        "payload": {"actor": "scheduler"},
    })
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]
    changed = schedule_client.patch(f"/api/v1/scheduled-tasks/{task_id}", json={"name": "Reviewed"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["payload"] == {"actor": "scheduler"}


def test_disabled_vendor_api_does_not_execute_and_can_be_reenabled(schedule_client, db_session, monkeypatch):
    from app.services import akshare_registry
    from app.services.akshare_utils import call_akshare_with_retry

    monkeypatch.setattr(akshare_registry, "_config_cache", {})
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", False)
    akshare_registry.load_config_cache(db_session)
    response = schedule_client.put("/api/v1/external-data/apis/stock_zh_a_hist", json={"enabled": False})
    assert response.status_code == 200, response.text

    def forbidden_transport():
        pytest.fail("disabled provider was contacted")

    with pytest.raises(RuntimeError, match="disabled"):
        call_akshare_with_retry(forbidden_transport, api_key="stock_zh_a_hist", max_attempts=1)
    response = schedule_client.put("/api/v1/external-data/apis/stock_zh_a_hist", json={"enabled": True, "anti_risk_strategy": "custom", "delay_min_ms": 0, "delay_max_ms": 0})
    assert response.status_code == 200, response.text
    assert call_akshare_with_retry(lambda: "supplier data", api_key="stock_zh_a_hist", max_attempts=1) == "supplier data"


@pytest.mark.parametrize("future_days", [1, 3, 5, 10, 20])
def test_signal_statistics_use_only_mature_horizons(schedule_client, db_session, future_days):
    from datetime import date
    from app.models.daily_bar import DailyBar
    from app.models.score import Score
    from app.models.symbol import Symbol

    symbol = Symbol(symbol="600999", name="Maturity", asset_type="stock", market="sh")
    db_session.add(symbol)
    db_session.flush()
    for day in (1, 30):
        db_session.add(Score(symbol_id=symbol.id, trade_date=date(2026, 10, day), quality_score=80, quality_grade="A", timing_score=80, priority_score=80, stage="bull", action="buy"))
    for day in range(1, future_days + 2):
        close = 9 + day
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026, 10, day), open=close, high=close, low=close, close=close, volume=100))
    db_session.commit()
    response = schedule_client.get(f"/api/v1/signal-rules/stats/{symbol.id}")
    assert response.status_code == 200, response.text
    result = response.json()
    expected_returns = {3: 0.3, 5: 0.5, 10: 1.0, 20: 2.0}
    for horizon in (3, 5, 10, 20):
        assert result[f"avg_return_{horizon}d"] == (expected_returns[horizon] if future_days >= horizon else None)
        assert result[f"win_rate_{horizon}d"] == (1.0 if future_days >= horizon else None)


@pytest.mark.parametrize("formula", ["1 / 0", "sma(20)", "sma(20) < close", "macd(12,26,9)"])
def test_formula_preview_reports_computation_failure(schedule_client, db_session, formula):
    from datetime import date
    from app.models.daily_bar import DailyBar
    from app.models.symbol import Symbol

    symbol = Symbol(symbol="600998", name="Formula", asset_type="stock", market="sh")
    db_session.add(symbol)
    db_session.flush()
    db_session.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026, 10, 1), open=10, high=11, low=9, close=10, volume=100))
    db_session.commit()
    response = schedule_client.post("/api/v1/settings/custom-indicators/preview", json={"symbol_id": symbol.id, "formula": formula, "value_type": "number"})
    assert response.status_code == 400, response.text
    assert "formula" in str(response.json()["detail"]).lower()


@pytest.mark.parametrize("formula,value_type,result_key,expected", [("close - close", "number", "result_number", 0.0), ("close < 0", "boolean", "result_boolean", False)])
def test_formula_preview_preserves_real_zero_and_false(schedule_client, db_session, formula, value_type, result_key, expected):
    from datetime import date
    from app.models.daily_bar import DailyBar
    from app.models.symbol import Symbol

    symbol = Symbol(symbol="600998", name="Formula", asset_type="stock", market="sh")
    db_session.add(symbol)
    db_session.flush()
    db_session.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026, 10, 1), open=10, high=11, low=9, close=10, volume=100))
    db_session.commit()
    response = schedule_client.post("/api/v1/settings/custom-indicators/preview", json={"symbol_id": symbol.id, "formula": formula, "value_type": value_type})
    assert response.status_code == 200, response.text
    assert response.json()[result_key] == expected


@pytest.mark.parametrize("enabled", [True, False])
def test_discovery_formula_errors_remain_uncomputed(schedule_client, db_session, enabled):
    from datetime import date
    from app.models.custom_indicator import CustomIndicator
    from app.models.daily_bar import DailyBar
    from app.models.scan import ScanResult, ScanRun
    from app.models.symbol import Symbol

    symbol = Symbol(symbol="600997", name="Discovery", asset_type="stock", market="sh")
    db_session.add(symbol)
    run = ScanRun(run_name="isolated", scope_snapshot="{}", status="done")
    db_session.add(run)
    db_session.flush()
    row = ScanResult(scan_run_id=run.id, symbol_id=symbol.id, result_type="executable", rank_no=1)
    db_session.add(row)
    db_session.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026, 10, 1), open=10, high=11, low=9, close=10, volume=100))
    for key, formula, value_type in (("broken", "1 / 0", "number"), ("short", "sma(20) > close", "boolean"), ("real_zero", "close - close", "number"), ("real_false", "close < 0", "boolean")):
        db_session.add(CustomIndicator(name=key, key=key, formula=formula, value_type=value_type, enabled=enabled, scope_json='["discovery"]', params_json="[]"))
    db_session.commit()
    response = schedule_client.post("/api/v1/discovery/indicators/evaluate", json={"scan_result_ids": [row.id], "indicator_keys": ["broken", "short", "real_zero", "real_false"]})
    assert response.status_code == 200, response.text
    result = response.json()[0]
    assert result["values"] == ({"broken": None, "short": None, "real_zero": 0.0, "real_false": False} if enabled else {"broken": None, "short": None, "real_zero": None, "real_false": None})
    assert set(result["errors"]) == ({"broken", "short"} if enabled else {"broken", "short", "real_zero", "real_false"})


def test_saved_scoring_direction_changes_standard_score(schedule_client, db_session):
    from datetime import date
    from app.models.daily_bar import DailyBar
    from app.models.scoring_config import ScoringConfig
    from app.models.symbol import Symbol
    from app.services.scoring_config_engine import calculate_symbol_score_with_config

    symbol = Symbol(symbol="600996", name="Stable", asset_type="stock", market="sh")
    db_session.add(symbol)
    db_session.flush()
    for day in range(1, 6):
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026, 10, day), open=10, high=10, low=10, close=10, volume=100))
    db_session.commit()
    observed = []
    for direction in ("higher_better", "lower_better"):
        response = schedule_client.post("/api/v1/settings/scoring-configs", json={
            "asset_type": "stock", "preset_key": direction, "name": direction,
            "config": {"final_weights": {"quality": 1, "timing": 0, "news": 0}, "dimensions": [{"key": "risk", "score_bucket": "quality", "weight": 1, "factors": [{"key": "volatility_score", "source": "builtin", "weight": 1, "direction": direction}]}]},
        })
        assert response.status_code == 200, response.text
        config = db_session.get(ScoringConfig, response.json()["id"])
        score = calculate_symbol_score_with_config(db_session, symbol, date(2026, 10, 5), config)
        observed.append(score.quality_score)
    assert observed == [70.0, 30.0]

    # Simulate a persisted pre-contract configuration whose direction was only
    # descriptive metadata. Reading or rescoring must preserve its old result.
    import json
    legacy = json.loads(config.config_json)
    legacy.pop("direction_contract")
    config.config_json = json.dumps(legacy)
    db_session.commit()
    response = schedule_client.get("/api/v1/settings/scoring-configs", params={"asset_type": "stock"})
    editable = next(row for row in response.json() if row["id"] == config.id)["config"]
    assert editable["direction_contract"] == "standard_score_v1"
    assert editable["dimensions"][0]["factors"][0]["direction"] == "higher_better"
    assert calculate_symbol_score_with_config(db_session, symbol, date(2026, 10, 5), config).quality_score == 70.0


def test_name_refresh_respects_disabled_vendor_endpoints(schedule_client, db_session, monkeypatch):
    from app.services import akshare_registry, symbol_names

    monkeypatch.setattr(akshare_registry, "_config_cache", {})
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", False)
    akshare_registry.load_config_cache(db_session)
    for key in ("stock_info_a_code_name", "fund_name_em"):
        response = schedule_client.put(f"/api/v1/external-data/apis/{key}", json={"enabled": False})
        assert response.status_code == 200
    symbol_names._cn_stock_name_map.cache_clear()
    symbol_names._fund_name_map.cache_clear()

    def forbidden_transport():
        pytest.fail("disabled name provider was contacted")

    monkeypatch.setattr(symbol_names.ak, "stock_info_a_code_name", forbidden_transport)
    monkeypatch.setattr(symbol_names.ak, "fund_name_em", forbidden_transport)
    assert symbol_names.resolve_symbol_name("600001", "stock", "sh", "600001") == "600001"


@pytest.mark.parametrize("direction", ["range_better", "lower_or_range_better"])
def test_scoring_api_rejects_unimplemented_range_direction(schedule_client, direction):
    response = schedule_client.post("/api/v1/settings/scoring-configs", json={"asset_type": "stock", "preset_key": "invalid_range", "name": "Range", "config": {"dimensions": [{"key": "risk", "factors": [{"key": "volatility_score", "direction": direction}]}]}})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("formula,expected_trades", [("sma(20)", 0), ("close < 0", 1)])
def test_backtest_does_not_compare_uncomputed_formula_as_false(db_session, formula, expected_trades):
    from datetime import date, datetime
    from app.models.daily_bar import DailyBar
    from app.models.portfolio import Portfolio
    from app.models.score import Score
    from app.models.symbol import Symbol
    from app.services.backtest import run_backtest

    portfolio = Portfolio(name="Formula backtest", account_type="simulated", total_capital=100000, investable_ratio=0.9, cash_reserve_ratio=0.1)
    symbol = Symbol(symbol="600995", name="Backtest", asset_type="stock", market="sh")
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    for day in range(1, 4):
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026, 10, day), open=10, high=11, low=9, close=10, volume=10000))
        db_session.add(Score(symbol_id=symbol.id, trade_date=date(2026, 10, day), quality_score=80, quality_grade="A", timing_score=80, priority_score=80, stage="bull", action="buy", published_at=datetime(2026, 9, 30)))
    db_session.commit()
    rule = {"version": 2, "buy_conditions": {"logic": "AND", "conditions": [{"field": "custom_expr", "operator": "eq", "value": False, "params": {"expr": formula}}]}, "sell_conditions": {"logic": "AND", "conditions": [{"field": "close", "operator": "lt", "value": 0}]}}
    result = run_backtest(db_session, portfolio.id, [symbol.id], date(2026, 10, 1), date(2026, 10, 3), rule)
    assert result.status == "completed"
    assert result.trade_count == expected_trades
