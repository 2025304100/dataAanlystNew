"""Public AI HTTP contracts with isolated SQLite and an offline provider transport."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import ai_profiles, ai_sessions
from app.services.ai_failover import reset_failover_manager


@pytest.fixture
def ai_client(db_session):
    reset_failover_manager()
    app = FastAPI()
    app.include_router(ai_profiles.router)
    app.include_router(ai_sessions.router)
    yield TestClient(app)
    reset_failover_manager()


def make_profile(client, **overrides):
    response = client.post("/ai/profiles", json={
        "name": "primary", "provider": "openai_compatible", "model": "offline",
        "base_url": "https://offline.example/v1", "auth_type": "none", **overrides,
    })
    assert response.status_code == 200, response.text
    return response.json()


def install_transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def stream_reply():
    answer = json.dumps({"answer": "offline answer", "evidence": [], "warnings": [], "suggested_actions": []})
    return "data: " + json.dumps({"choices": [{"delta": {"content": answer}}]}) + "\n\ndata: [DONE]\n\n"


def provider_reply(request):
    if json.loads(request.content).get("stream"):
        return httpx.Response(200, text=stream_reply())
    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})


@pytest.mark.parametrize("path", ["/ai/sessions", "/ai/sessions/stream"])
def test_streaming_assistant_records_usage_and_honors_pinned_session_daily_quota(ai_client, monkeypatch, path):
    profile = make_profile(ai_client, daily_request_limit=1)
    requests = []
    def provider(request):
        requests.append(request)
        return provider_reply(request)
    install_transport(monkeypatch, provider)
    first = ai_client.post(path, json={"first_message": "hello"})
    if path.endswith("stream"):
        assert "event: done" in first.text
        session_event = first.text.split("event: session\ndata: ")[1].split("\n")[0]
        session_id = json.loads(session_event)["session_id"]
    else:
        assert first.status_code == 200
        session_id = first.json()["session_id"]
    usage = ai_client.get(f"/ai/profiles/{profile['id']}/usage").json()
    assert usage["daily_request_count"] == 1
    second = ai_client.post("/ai/sessions/stream", json={"session_id": session_id, "first_message": "again"})
    assert "event: done" not in second.text
    assert len(requests) == 1


def test_streaming_assistant_uses_backup_before_output_and_reports_actual_model(ai_client, monkeypatch):
    primary = make_profile(ai_client)
    backup = make_profile(ai_client, name="backup", model="offline-backup", is_fallback=True, priority=10)
    def provider(request):
        if json.loads(request.content)["model"] == "offline":
            return httpx.Response(503, text="unavailable")
        return provider_reply(request)
    install_transport(monkeypatch, provider)
    response = ai_client.post("/ai/sessions/stream", json={"first_message": "hello"})
    assert "event: done" in response.text
    assert '"model_used": "offline-backup"' in response.text
    assert next(p for p in ai_client.get("/ai/profiles").json() if p["id"] == primary["id"])["health_status"] == "degraded"
    assert ai_client.get(f"/ai/profiles/{backup['id']}/usage").json()["daily_request_count"] == 1


@pytest.mark.parametrize("limits", [{"max_concurrent": 1}, {"max_concurrent": 3, "daily_request_limit": 1}])
def test_profile_concurrency_limit_covers_stream_and_connection_test(ai_client, monkeypatch, limits):
    profile = make_profile(ai_client, **limits)
    requests = []
    nested_results = []
    def provider(request):
        requests.append(request)
        if len(requests) == 1:
            nested_results.append(ai_client.post(f"/ai/profiles/{profile['id']}/test").json())
        return provider_reply(request)
    install_transport(monkeypatch, provider)
    response = ai_client.post("/ai/sessions/stream", json={"first_message": "hello"})
    assert "event: done" in response.text
    assert nested_results[0]["success"] is False
    assert len(requests) == 1


def test_connection_tests_consume_the_same_daily_budget_as_assistant_requests(ai_client, monkeypatch):
    profile = make_profile(ai_client, daily_request_limit=1)
    requests = []
    def provider(request):
        requests.append(request)
        return provider_reply(request)
    install_transport(monkeypatch, provider)
    assert ai_client.post(f"/ai/profiles/{profile['id']}/test").json()["success"] is True
    assert ai_client.get(f"/ai/profiles/{profile['id']}/usage").json()["daily_request_count"] == 1
    second = ai_client.post(f"/ai/profiles/{profile['id']}/test")
    assert second.json()["success"] is False
    assert len(requests) == 1


def test_native_unsupported_provider_is_rejected_instead_of_saved_as_usable(ai_client):
    response = ai_client.post("/ai/profiles", json={"name": "native", "provider": "anthropic", "model": "claude", "base_url": "https://offline.example/v1", "auth_type": "none"})
    assert response.status_code == 400
    assert ai_client.get("/ai/profiles").json() == []


def test_default_assistant_selects_chat_purpose_profile(ai_client, monkeypatch):
    make_profile(ai_client, purpose="chat")
    install_transport(monkeypatch, lambda request: httpx.Response(200, text=stream_reply()))
    response = ai_client.post("/ai/sessions/stream", json={"first_message": "hello"})
    assert "event: done" in response.text


def test_backup_selection_skips_profiles_with_no_daily_budget(ai_client, monkeypatch):
    make_profile(ai_client)
    make_profile(ai_client, name="exhausted", model="exhausted", is_fallback=True, priority=10, daily_request_limit=0)
    make_profile(ai_client, name="available", model="available", is_fallback=True, priority=20)
    requests = []
    def provider(request):
        model = json.loads(request.content)["model"]
        requests.append(model)
        return httpx.Response(503) if model == "offline" else httpx.Response(200, text=stream_reply())
    install_transport(monkeypatch, provider)
    response = ai_client.post("/ai/sessions/stream", json={"first_message": "hello"})
    assert "event: done" in response.text
    assert requests == ["offline", "available"]


def test_stream_failure_after_output_never_mixes_backup_and_releases_the_slot(ai_client, monkeypatch):
    primary = make_profile(ai_client, max_concurrent=1)
    make_profile(ai_client, name="backup", model="backup", is_fallback=True, priority=10)
    requests = []
    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self):
            chunk = {"choices": [{"delta": {"content": '{"answer":"partial'}}]}
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            raise httpx.ReadError("offline interrupted stream")
    def provider(request):
        requests.append(json.loads(request.content)["model"])
        return httpx.Response(200, stream=InterruptedStream()) if len(requests) == 1 else provider_reply(request)
    install_transport(monkeypatch, provider)
    first = ai_client.post("/ai/sessions/stream", json={"first_message": "hello"})
    assert "event: delta" in first.text
    assert "event: error" in first.text
    assert "event: done" not in first.text
    assert requests == ["offline"]
    second = ai_client.post(f"/ai/profiles/{primary['id']}/test")
    assert second.json()["success"] is True
    assert requests == ["offline", "offline"]


def test_empty_provider_stream_is_a_failure_and_can_use_backup_before_output(ai_client, monkeypatch):
    make_profile(ai_client)
    make_profile(ai_client, name="backup", model="backup", is_fallback=True, priority=10)
    def provider(request):
        return httpx.Response(200, text="") if json.loads(request.content)["model"] == "offline" else httpx.Response(200, text=stream_reply())
    install_transport(monkeypatch, provider)
    response = ai_client.post("/ai/sessions/stream", json={"first_message": "hello"})
    assert '"model_used": "backup"' in response.text


def test_connection_test_rejects_wrong_protocol_and_records_failure_health(ai_client, monkeypatch):
    profile = make_profile(ai_client)
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={"unexpected": "native response"}))
    response = ai_client.post(f"/ai/profiles/{profile['id']}/test")
    assert response.json()["success"] is False
    assert ai_client.get("/ai/profiles").json()[0]["health_status"] == "degraded"


def test_legacy_native_profile_remains_visible_but_test_does_not_issue_a_request(ai_client, db_session, monkeypatch):
    from app.models.ai_profile import AIProfile
    db_session.add(AIProfile(name="legacy-native", provider="anthropic", model="legacy", base_url="https://offline.example/v1", auth_type="none"))
    db_session.commit()
    profile = ai_client.get("/ai/profiles").json()[0]
    requests = []
    install_transport(monkeypatch, lambda request: requests.append(request) or provider_reply(request))
    response = ai_client.post(f"/ai/profiles/{profile['id']}/test")
    assert response.json()["success"] is False
    assert requests == []
    assert ai_client.get("/ai/profiles").json()[0]["provider"] == "anthropic"


def test_clean_stream_eof_without_completion_is_error_after_partial_output(ai_client, monkeypatch):
    make_profile(ai_client)
    make_profile(ai_client, name="backup", is_fallback=True, priority=10)
    requests = []
    partial = 'data: ' + json.dumps({"choices": [{"delta": {"content": '{"answer":"partial'}}]}) + '\n\n'
    def provider(request):
        requests.append(request)
        return httpx.Response(200, text=partial)
    install_transport(monkeypatch, provider)
    response = ai_client.post("/ai/sessions/stream", json={"first_message": "hello"})
    assert "event: error" in response.text
    assert "event: done" not in response.text
    assert len(requests) == 1


@pytest.mark.parametrize("path", ["/ai/sessions", "/ai/sessions/stream"])
def test_malformed_provider_protocol_returns_controlled_failure(ai_client, monkeypatch, path):
    make_profile(ai_client)
    if path.endswith("stream"):
        body = 'data: ' + json.dumps({"choices": [{"delta": {"content": "OK"}}], "usage": {"prompt_tokens": "invalid"}}) + '\n\ndata: [DONE]\n\n'
        install_transport(monkeypatch, lambda request: httpx.Response(200, text=body))
    else:
        install_transport(monkeypatch, lambda request: httpx.Response(200, json={"choices": ["wrong"]}))
    response = ai_client.post(path, json={"first_message": "hello"})
    if path.endswith("stream"):
        assert "event: error" in response.text
        assert "event: done" not in response.text
    else:
        assert response.status_code == 503
