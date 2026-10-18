"""AI readiness through public HTTP APIs, with every provider request isolated."""

import httpx
import json
import pytest
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import ai_config, system
from app.db.session import get_db

pytestmark = pytest.mark.whitebox


@pytest.fixture
def readiness_client(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_config, "AI_CONFIG_PATH", tmp_path / "ai_config.json")
    responses = []
    original_client = httpx.Client

    def provider(request):
        assert request.url.host == "offline.example"
        return responses.pop(0) if responses else httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    def isolated_provider_client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(provider))

    app = FastAPI()
    app.include_router(ai_config.router, prefix="/api/v1")
    app.include_router(system.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        monkeypatch.setattr(ai_config.httpx, "Client", isolated_provider_client)
        yield client, responses


def _ai_status(client):
    response = client.get("/api/v1/system/capabilities")
    assert response.status_code == 200, response.text
    body = response.json()
    capabilities = body.get("capabilities", body)
    if isinstance(capabilities, dict):
        return capabilities["ai"]
    return next(item for item in capabilities if item["key"] == "ai")


def test_auth_none_requires_a_real_matching_test_before_becoming_ready(readiness_client):
    client, _responses = readiness_client
    config = {"provider": "openai_compatible", "service_url": "https://offline.example/v1", "auth_type": "none", "api_key": "", "model": "isolated-model", "enabled": True}
    saved = client.put("/api/v1/settings/ai-config", json=config)
    assert saved.status_code == 200, saved.text
    assert _ai_status(client)["status"] == "degraded"
    tested = client.post("/api/v1/settings/ai-config/test", json=config)
    assert tested.status_code == 200 and tested.json()["success"] is True
    assert client.get("/api/v1/settings/ai-config").json()["connection_test"]["status"] == "passed"
    assert _ai_status(client)["status"] == "ready"


def test_legacy_url_formatting_matches_the_request_that_was_actually_tested(readiness_client, tmp_path):
    client, _responses = readiness_client
    config = _saved_and_tested(client)
    # A legacy persisted file may retain the optional trailing slash. The
    # execution endpoint strips it, so it must not produce a different proof.
    path = tmp_path / "ai_config.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["service_url"] += "/"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert client.post("/api/v1/settings/ai-config/test", json={**config, "service_url": legacy["service_url"]}).json()["success"] is True
    assert _ai_status(client)["status"] == "ready"


def _saved_and_tested(client):
    config = {"provider": "openai_compatible", "service_url": "https://offline.example/v1", "auth_type": "bearer", "api_key": "isolated-secret-one", "model": "isolated-model", "enabled": True}
    assert client.put("/api/v1/settings/ai-config", json=config).status_code == 200
    assert client.post("/api/v1/settings/ai-config/test", json=config).json()["success"] is True
    return config


@pytest.mark.parametrize("change", [
    {"provider": "custom"}, {"service_url": "https://offline.example/v2"},
    {"api_key": "isolated-secret-two"}, {"model": "different-model"},
    {"auth_type": "x-api-key"}, {"auth_header": "X-Custom-Key"},
    {"chat_path": "/other-chat"}, {"models_path": "/other-models"},
    {"timeout_seconds": 45}, {"temperature": 0.7}, {"max_tokens": 128},
    {"extra_headers": {"x-region": "isolated"}},
])
def test_changing_request_configuration_requires_its_own_test(readiness_client, change):
    client, _responses = readiness_client
    _saved_and_tested(client)
    assert client.put("/api/v1/settings/ai-config", json=change).status_code == 200
    public = client.get("/api/v1/settings/ai-config").json()
    assert public["connection_test"]["status"] == "untested"
    assert "isolated-secret-one" not in str(public)
    assert "isolated-secret-two" not in str(public)
    assert _ai_status(client)["status"] == "degraded"


def test_unsaved_failed_test_does_not_replace_another_configurations_proof(readiness_client):
    client, responses = readiness_client
    config = _saved_and_tested(client)
    responses.append(httpx.Response(401, json={"error": "isolated unauthorized"}))
    failed = client.post("/api/v1/settings/ai-config/test", json={**config, "model": "unsaved-model"})
    assert failed.json()["success"] is False
    current = client.get("/api/v1/settings/ai-config").json()
    assert current["model"] == "isolated-model"
    assert current["connection_test"]["status"] == "passed"
    assert _ai_status(client)["status"] == "ready"


def test_many_unsaved_failures_do_not_evict_the_saved_configuration_proof(readiness_client):
    client, responses = readiness_client
    config = _saved_and_tested(client)
    for index in range(10):
        responses.append(httpx.Response(401, json={"error": "isolated unauthorized"}))
        assert client.post("/api/v1/settings/ai-config/test", json={**config, "model": f"unsaved-{index}"}).json()["success"] is False
    assert _ai_status(client)["status"] == "ready"


def test_a_protocol_error_revokes_the_matching_success_proof(readiness_client):
    client, responses = readiness_client
    config = _saved_and_tested(client)
    responses.append(httpx.Response(200, json={"error": "not a model reply"}))
    failed = client.post("/api/v1/settings/ai-config/test", json=config)
    assert failed.json()["success"] is False
    assert client.get("/api/v1/settings/ai-config").json()["connection_test"]["status"] == "failed"
    assert _ai_status(client)["status"] == "degraded"


def test_offset_test_time_is_normalized_and_success_expires_after_seven_days(readiness_client, monkeypatch):
    client, _responses = readiness_client

    class Clock(datetime):
        current = datetime(2026, 10, 15, 3, tzinfo=timezone(timedelta(hours=8)))

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(ai_config, "datetime", Clock)
    _saved_and_tested(client)
    ready = _ai_status(client)
    assert ready["status"] == "ready"
    assert ready["data_cutoff_at"] == "2026-10-14T19:00:00"
    Clock.current += timedelta(days=8)
    assert client.get("/api/v1/settings/ai-config").json()["connection_test"]["status"] == "expired"
    assert _ai_status(client)["status"] == "degraded"
