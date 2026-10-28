"""Temporary acceptance mode through real production HTTP routes."""
import pytest
from fastapi.testclient import TestClient

from app.models.symbol import Symbol
from app.models.ai_profile import AIProfile
from app.models.notification import NotificationOutbox
from app.models.market_signal import MarketSignalAssessment, MarketSignalPrediction

pytestmark = pytest.mark.whitebox


def test_validation_runtime_allows_rule_crud_and_blocks_actions(db_session, monkeypatch):
    import app.main as main
    db_session.add(Symbol(symbol="000001", name="验收银行", market="SZ", asset_type="stock"))
    db_session.commit()
    before_profiles = db_session.query(AIProfile).count()
    monkeypatch.setenv("DATABASE_URL", db_session.get_bind().url.render_as_string(hide_password=False))
    monkeypatch.setattr(main.settings, "APP_VALIDATION_MODE", True, raising=False)
    monkeypatch.setattr(main.settings, "AUTO_UNIVERSE_SYNC_ON_STARTUP", False)
    monkeypatch.setenv("AUTO_TRADE_CALENDAR_REFRESH", "0")
    with TestClient(main.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.headers.get("X-App-Validation-Mode") == "1"
        assert client.get("/api/v1/market-signals/settings").json()["enabled"] is False
        saved = client.post("/api/v1/market-signals/rules", json={"name":"受控验收规则", "target_type":"symbol",
            "target_key":"000001", "enabled":False, "mode":"shadow", "min_score":95})
        assert saved.status_code == 201, saved.text
        rule_id = saved.json()["id"]
        updated = client.put(f"/api/v1/market-signals/rules/{rule_id}", json={"window_days":5})
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == 2
        assert client.get("/api/v1/market-signals/rules").json()[0]["window_days"] == 5
        assert len(client.get(f"/api/v1/market-signals/rules/{rule_id}/versions").json()) == 2
        for path, body in (("/api/v1/market-signals/analyze", {}),
                ("/api/v1/market-signals/jobs", {"kind":"monitor"}),
                ("/api/v1/notifications/channels/1/test", {}),
                ("/api/v1/market-signals/settings", {"enabled":True})):
            response = client.put(path, json=body) if path.endswith("/settings") else client.post(path,json=body)
            assert response.status_code == 409, (path,response.text)
        db_session.expire_all()
        assert db_session.query(AIProfile).count() == before_profiles
        assert db_session.query(NotificationOutbox).count() == 0
        assert db_session.query(MarketSignalAssessment).count() == 0
        assert db_session.query(MarketSignalPrediction).count() == 0
