"""Real MySQL revision lifecycle and concurrent public processing seam."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text, select
from sqlalchemy.orm import Session

from tests.market_news_acceptance_support import isolated_url, engine_for, assert_identity, migrate, evidence, REV91

REV92 = "wps_0024_092_market_signals"
pytestmark = [pytest.mark.whitebox, pytest.mark.skipif(not os.environ.get("MARKET_SIGNALS_MYSQL57_URL"),
    reason="Use scripts/verify_market_signals_acceptance.py with a fresh isolated schema")]


@pytest.fixture(scope="module")
def native_engine():
    url = isolated_url("MARKET_SIGNALS_MYSQL57_URL")
    engine = engine_for(url)
    with engine.connect() as connection:
        identity = assert_identity(connection, url)
        assert not inspect(connection).get_table_names()
    migrate(url, REV91)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO app_settings (`key`,value_json,updated_by,updated_at) "
            "VALUES ('signals.migration.sentinel','\"历史设置不变\"','fixture',NOW())"))
    migrate(url, REV92)
    from app.models.market_signal import MarketSignalEvidence
    with Session(engine) as db:
        db.add(MarketSignalEvidence(fingerprint="f"*64, title="迁移中文验收🚀", text="已送达证据不丢失"*3000,
            source="manual", time_quality="unknown", kind="news", metadata_json="{}"))
        db.commit()
    migrate(url, REV91, downgrade=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT text FROM market_signal_evidence")) == "已送达证据不丢失"*3000
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REV91
    migrate(url, REV92)
    migrate(url, REV92)
    with engine.connect() as connection:
        tables = [name for name in inspect(connection).get_table_names() if name.startswith("market_signal_")]
        assert len(tables) == 7
        assert connection.scalar(text("SELECT value_json FROM app_settings WHERE `key`='signals.migration.sentinel'")) == '"历史设置不变"'
        engines = dict(connection.execute(text("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'market_signal_%'")).all())
        assert set(engines.values()) == {"InnoDB"}
    evidence("signals-mysql57-lifecycle.json", {"identity": identity, "revision": REV92,
        "tables": tables, "engines": engines, "rollback_evidence_retained": True,
        "legacy_sentinel_preserved": True, "reupgrade_idempotent": True})
    yield engine
    engine.dispose()


def test_0092_mysql57_upgrade_rollback_reupgrade_preserves_evidence(native_engine):
    with native_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REV92
        assert connection.scalar(text("SELECT COUNT(*) FROM market_signal_evidence")) == 1


def test_concurrent_public_run_creates_one_prediction(native_engine, monkeypatch):
    from app.api.routes.market_signals import router
    from app.db.session import get_db
    from app.models.market_signal import MarketSignalAssessment, MarketSignalEvidence, MarketSignalRule, MarketSignalRuleVersion, MarketSignalSettings
    from app.models.symbol import Symbol
    from app.models.daily_bar import DailyBar
    from app.models.market_data import MarketCalendar
    from app.services import market_signal_sources
    now = datetime(2026,10,27,9,10)
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now)
    config = {"name": "并发关注", "target_type": "symbol", "target_key": "000001", "quote_key": "000001", "quote_type": "symbol",
        "min_score": 85, "window_days": 1, "min_move_pct": 2, "adaptive_threshold": False, "volatility_percentile": 80,
        "max_age_hours": 24, "cooldown_hours": 6, "max_daily": 1, "mode": "shadow", "delivery_mode": "instant", "digest_time": "16:10",
        "enabled": True, "channel_ids": []}
    with Session(native_engine) as db:
        stock = Symbol(symbol="000001", name="并发银行", asset_type="stock", market="SZ")
        rule = MarketSignalRule(name=config["name"], target_type="symbol", target_key="000001", config_json=json.dumps(config))
        item = MarketSignalEvidence(fingerprint="e"*64, title="并发订单正式落地", text="正式新增订单100亿元",
            source="manual", source_url="https://example.com/order", published_at=now, time_quality="manual", kind="news", metadata_json="{}")
        db.add_all([stock, rule, item, MarketSignalSettings(id=1, config_json='{"enabled":true}')])
        db.flush()
        for offset in range(10):
            day = date(2026,10,27)+timedelta(days=offset)
            db.add(MarketCalendar(trade_date=day, is_trading_day=int(day.weekday()<5)))
        db.add(DailyBar(symbol_id=stock.id, trade_date=date(2026,10,27), open=100, high=101, low=99, close=100, source="fixture:qfq"))
        db.add(MarketSignalAssessment(evidence_id=item.id, rule_id=rule.id, rule_revision=1, request_key="r"*64,
            status="scored", score=90, event_key="a"*64, fact_key="b"*64, analysis_json='{"novelty_points":30}', input_json="{}"))
        db.commit()
    app = FastAPI()
    app.include_router(router)
    def isolated_db():
        with Session(native_engine) as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    def invoke(_):
        with TestClient(app) as client:
            response = client.post("/market-signals/run", json={"analyze": False})
            if response.status_code == 409:
                return 0
            assert response.status_code == 200, response.text
            return response.json()["created"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(invoke, range(2)))
    assert sorted(created) == [0, 1]
    evidence("signals-mysql57-concurrent.json", {"created_per_caller": created, "prediction_count": sum(created)})
