"""Settings notification contracts through the HTTP API and network boundary."""
from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.services.notifications.anti_disturb import is_in_quiet_hours

pytestmark = pytest.mark.whitebox


@pytest.fixture
def client(db_session):
    from app.main import app

    def session_override():
        yield db_session

    app.dependency_overrides[get_db] = session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_editing_channel_preserves_unchanged_masked_credentials(client, monkeypatch):
    secret = "AUDIT_FAKE_TOKEN_123456789"
    created = client.post("/api/v1/notifications/channels", json={
        "name": "existing-token", "channel_type": "wxpusher",
        "config": {"app_token": secret, "uids": ["UID_audit"]},
    })
    assert created.status_code == 200, created.text
    channel = created.json()
    masked = json.loads(channel["config_mask_json"])
    renamed = client.patch(f"/api/v1/notifications/channels/{channel['id']}", json={
        "name": "renamed-token", "config": masked,
    })
    assert renamed.status_code == 200, renamed.text
    sent = []

    def provider(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 1000, "msg": "ok", "data": []})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(provider), **kwargs,
    ))
    tested = client.post(f"/api/v1/notifications/channels/{channel['id']}/test")
    assert tested.status_code == 200, tested.text
    assert sent[0]["appToken"] == secret
    assert tested.json()["success"] is True
    enabled = client.patch(f"/api/v1/notifications/channels/{channel['id']}", json={"enabled": True})
    assert enabled.json()["enabled"] is True
    unchanged = client.patch(f"/api/v1/notifications/channels/{channel['id']}", json={
        "name": "rename-without-retest", "config": masked,
    })
    assert unchanged.json()["enabled"] is True
    assert unchanged.json()["status"] == "enabled"


def test_optional_channel_credential_can_be_explicitly_cleared(client):
    channel = client.post("/api/v1/notifications/channels", json={
        "name": "clear-credential", "channel_type": "webhook",
        "config": {"url": "https://example.invalid/notify", "auth_header": "Bearer FAKE_SECRET"},
    }).json()
    updated = client.patch(f"/api/v1/notifications/channels/{channel['id']}", json={"clear_config_keys": ["auth_header"]})
    assert updated.status_code == 200, updated.text
    assert "auth_header" not in json.loads(updated.json()["config_mask_json"])


def test_unimplemented_digest_delivery_cannot_be_saved_as_operational(client):
    response = client.post("/api/v1/notifications/policies", json={
        "name": "digest-is-not-live", "delivery_mode": "digest", "enabled": True,
        "source_types": ["trade_executed"],
    })
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("utc_time, expected", [(datetime(2026, 10, 15, 15), True), (datetime(2026, 10, 15, 0), False)])
def test_quiet_hours_observe_configured_local_time(utc_time, expected):
    settings = json.dumps({"start": "22:00", "end": "08:00", "timezone": "Asia/Shanghai"})
    assert is_in_quiet_hours(settings, now=utc_time) is expected


def test_limited_notification_scope_requires_explicit_objects(client):
    response = client.post("/api/v1/notifications/policies", json={
        "name": "missing-scope-objects", "enabled": True,
        "source_types": ["trade_executed"], "scope_type": "portfolio", "scope_ids": [],
    })
    assert response.status_code == 422, response.text


def test_changing_policy_scope_cannot_keep_an_empty_object_set(client):
    policy = client.post("/api/v1/notifications/policies", json={
        "name": "scope-update", "source_types": ["trade_executed"],
    }).json()
    response = client.patch(f"/api/v1/notifications/policies/{policy['id']}", json={"scope_type": "portfolio"})
    assert response.status_code == 422, response.text


def test_onebot_uses_native_http_action_and_flat_parameters(client, monkeypatch):
    channel = client.post("/api/v1/notifications/channels", json={
        "name": "onebot-native", "channel_type": "onebot",
        "config": {"webhook": "https://example.invalid/", "target_type": "group", "target_id": "123456", "access_token": "FAKE_TOKEN"},
    }).json()
    observed = []

    def provider(request):
        observed.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 1}})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(provider), **kwargs))
    response = client.post(f"/api/v1/notifications/channels/{channel['id']}/test")
    assert response.json()["success"] is True
    assert observed[0]["path"] == "/send_group_msg"
    assert observed[0]["body"]["group_id"] == 123456
    assert "message" in observed[0]["body"]
    assert "params" not in observed[0]["body"]


def test_onebot_edited_target_type_overrides_legacy_message_type(client, monkeypatch):
    channel = client.post("/api/v1/notifications/channels", json={
        "name": "onebot-legacy-target", "channel_type": "onebot",
        "config": {"webhook": "https://example.invalid/", "target_type": "group", "target_id": "123456", "message_type": "group"},
    }).json()
    assert client.patch(f"/api/v1/notifications/channels/{channel['id']}", json={"config": {"target_type": "user", "target_id": "654321"}}).status_code == 200
    sent = []
    real_client = httpx.Client
    def provider(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "ok", "retcode": 0})
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(provider), **kwargs))
    assert client.post(f"/api/v1/notifications/channels/{channel['id']}/test").json()["success"] is True
    assert len(sent) == 1
    assert sent[0][0] == "/send_private_msg"
    assert sent[0][1]["user_id"] == 654321
    assert "group_id" not in sent[0][1]


def test_notification_capabilities_expose_only_connected_sources(client):
    response = client.get("/api/v1/notifications/capabilities")
    assert response.status_code == 200
    capabilities = response.json()
    events = {event["type"]: event for group in capabilities["sources"].values() for event in group["events"]}
    assert events["score_drop"]["supported"] is True
    assert events["task_complete"]["supported"] is False
    assert capabilities["delivery_modes"] == ["instant"]


def test_unconnected_notification_source_is_rejected_before_save(client):
    response = client.post("/api/v1/notifications/policies", json={
        "name": "not-a-live-source", "source_types": ["task_complete"], "enabled": True,
    })
    assert response.status_code == 400


@pytest.mark.parametrize("settings", [
    {"start": "25:00", "end": "08:00"},
    {"start": "22:00"},
    {"start": "22:00", "end": "08:00", "timezone": "not/a-zone"},
])
def test_invalid_quiet_hours_are_rejected_before_save(client, settings):
    response = client.post("/api/v1/notifications/policies", json={
        "name": "invalid-clock", "quiet_hours": settings,
    })
    assert response.status_code == 422, response.text


def test_corrupt_legacy_quiet_clock_blocks_delivery_without_crashing():
    assert is_in_quiet_hours(json.dumps({"start": "25:00", "end": "08:00"})) is True


def test_corrupt_legacy_quiet_config_cannot_bypass_critical_delivery():
    from app.services.notifications.anti_disturb import should_send_now
    from app.models.notification import NotificationPolicy
    for settings in ("{", "[]", '{"start":"22:00","end":"08:00","bypass_for_critical":"false"}'):
        policy = NotificationPolicy(quiet_hours_json=settings)
        allowed, _ = should_send_now(None, policy=policy, source_type="system", source_id=1,
                                    event_type="task_failed", channel_id=1, severity="critical", now=datetime(2026, 10, 15, 15))
        assert allowed is False


def test_policy_scope_cannot_be_cleared_to_an_unknown_scope(client):
    policy = client.post("/api/v1/notifications/policies", json={"name": "scope-blank"}).json()
    response = client.patch(f"/api/v1/notifications/policies/{policy['id']}", json={"scope_type": ""})
    assert response.status_code == 422
