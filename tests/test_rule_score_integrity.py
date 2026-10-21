"""Rule scoring cannot claim model inference or disguise stale input data."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes import scores
from app.db.base import Base
from app.db.session import get_db
from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol

pytestmark = pytest.mark.whitebox


@pytest.fixture
def scoring_client():
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        symbol = Symbol(symbol="RULE-INTEGRITY", name="Rule score fixture",
            asset_type="stock", market="SH")
        db.add(symbol)
        db.commit()
        app = FastAPI()
        app.include_router(scores.router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            yield client, db, symbol
    engine.dispose()


def add_bars(db, symbol, last_day):
    for offset in range(80):
        db.add(DailyBar(symbol_id=symbol.id,
            trade_date=last_day - timedelta(days=79-offset),
            open=10, close=10.1, high=10.2, low=9.9,
            volume=100000, amount=1000000, turnover_rate=2))
    db.commit()


@pytest.mark.parametrize("weight_mode", ["manual", "ridge"])
def test_rule_endpoint_cannot_attach_a_model_label(scoring_client, weight_mode):
    client, db, symbol = scoring_client
    add_bars(db, symbol, date.today())

    response = client.post("/api/v1/scores/calculate", json={
        "symbol_ids": [symbol.id], "trade_date": date.today().isoformat(),
        "weight_mode": weight_mode, "factor_model_run_id": "not-inferred-model",
        "factor_set_id": "not-used-factor-set",
    })

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "MODEL_SCORING_PIPELINE_REQUIRED"
    assert client.get(f"/api/v1/scores/history/{symbol.id}").json() == []


def test_requesting_today_does_not_make_old_quotes_fresh(scoring_client):
    client, db, symbol = scoring_client
    add_bars(db, symbol, date.today() - timedelta(days=30))

    response = client.post("/api/v1/scores/calculate", json={
        "symbol_ids": [symbol.id], "trade_date": date.today().isoformat(),
    })

    assert response.status_code == 200, response.text
    assert response.json()[0]["data_credibility"] == 0.1
    import json
    evidence = json.loads(response.json()[0]["factor_scores_json"])["_data_freshness"]
    assert evidence["latest_bar_trade_date"] == (date.today() - timedelta(days=30)).isoformat()


def freeze_clock(monkeypatch, value):
    from app.services import scoring_config_engine
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.replace(tzinfo=None) if tz is None else value.astimezone(tz)
    monkeypatch.setattr(scoring_config_engine, "datetime", FixedDatetime)


def test_weekend_uses_the_last_confirmed_trading_day(scoring_client, monkeypatch):
    from app.models.market_data import MarketCalendar
    client, db, symbol = scoring_client
    friday = date(2026, 8, 21)
    sunday = date(2026, 8, 23)
    freeze_clock(monkeypatch, datetime(2026, 8, 23, 8, tzinfo=timezone.utc))
    add_bars(db, symbol, friday)
    db.add_all([MarketCalendar(trade_date=friday, is_trading_day=1),
        MarketCalendar(trade_date=friday + timedelta(days=1), is_trading_day=0),
        MarketCalendar(trade_date=sunday, is_trading_day=0)])
    db.commit()

    response = client.post("/api/v1/scores/calculate", json={
        "symbol_ids": [symbol.id], "trade_date": sunday.isoformat(),
    })

    assert response.status_code == 200, response.text
    assert response.json()[0]["data_credibility"] == 1.0


def test_future_score_dates_are_rejected_without_publishing(scoring_client):
    client, db, symbol = scoring_client
    add_bars(db, symbol, date.today())

    response = client.post("/api/v1/scores/calculate", json={
        "symbol_ids": [symbol.id],
        "trade_date": (date.today() + timedelta(days=1)).isoformat(),
    })

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "SCORE_DATE_IN_FUTURE"
    assert client.get(f"/api/v1/scores/history/{symbol.id}").json() == []


def test_legacy_database_fallback_also_reports_stale_quotes(scoring_client):
    from sqlalchemy import text
    client, db, symbol = scoring_client
    add_bars(db, symbol, date.today() - timedelta(days=30))
    db.execute(text("DROP TABLE scoring_configs"))
    db.commit()

    response = client.post("/api/v1/scores/calculate", json={
        "symbol_ids": [symbol.id], "trade_date": date.today().isoformat(),
    })

    assert response.status_code == 200, response.text
    assert response.json()[0]["data_credibility"] == 0.1


def test_recalculating_a_legacy_rule_row_does_not_keep_its_false_model_label(scoring_client):
    from app.models.score import Score
    client, db, symbol = scoring_client
    add_bars(db, symbol, date.today())
    payload = {"symbol_ids": [symbol.id], "trade_date": date.today().isoformat()}
    first = client.post("/api/v1/scores/calculate", json=payload)
    assert first.status_code == 200
    legacy = db.get(Score, first.json()[0]["id"])
    legacy.weight_mode = "ridge"
    legacy.factor_model_run_id = "legacy-false-model-label"
    legacy.factor_set_id = "legacy-false-factor-label"
    db.commit()

    response = client.post("/api/v1/scores/calculate", json=payload)

    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert row["weight_mode"] == "manual"
    assert row["factor_model_run_id"] is None
    assert row["factor_set_id"] is None


def test_missing_calendar_dates_do_not_turn_a_gap_into_a_holiday(scoring_client, monkeypatch):
    from app.models.market_data import MarketCalendar
    client, db, symbol = scoring_client
    freeze_clock(monkeypatch, datetime(2026, 8, 23, 8, tzinfo=timezone.utc))
    add_bars(db, symbol, date(2026, 8, 21))
    db.add(MarketCalendar(trade_date=date(2026, 8, 21), is_trading_day=1))
    db.commit()

    response = client.post("/api/v1/scores/calculate", json={
        "symbol_ids": [symbol.id], "trade_date": "2026-08-23",
    })

    assert response.status_code == 200, response.text
    assert response.json()[0]["data_credibility"] == 0.8


def test_before_market_close_uses_the_previous_closed_day(scoring_client, monkeypatch):
    from app.models.market_data import MarketCalendar
    client, db, symbol = scoring_client
    freeze_clock(monkeypatch, datetime(2026, 8, 25, 2, tzinfo=timezone.utc))
    add_bars(db, symbol, date(2026, 8, 24))
    db.add_all([MarketCalendar(trade_date=date(2026, 8, 24), is_trading_day=1),
        MarketCalendar(trade_date=date(2026, 8, 25), is_trading_day=1)])
    db.commit()

    response = client.post("/api/v1/scores/calculate", json={
        "symbol_ids": [symbol.id], "trade_date": "2026-08-25",
    })

    assert response.status_code == 200, response.text
    assert response.json()[0]["data_credibility"] == 1.0


def test_empty_quotes_are_low_confidence_and_have_no_fake_source_day(scoring_client):
    import json
    client, _db, symbol = scoring_client

    response = client.post("/api/v1/scores/calculate", json={
        "symbol_ids": [symbol.id], "trade_date": date.today().isoformat(),
    })

    assert response.status_code == 200, response.text
    assert response.json()[0]["data_credibility"] == 0.2
    evidence = json.loads(response.json()[0]["factor_scores_json"])["_data_freshness"]
    assert evidence["latest_bar_trade_date"] is None
