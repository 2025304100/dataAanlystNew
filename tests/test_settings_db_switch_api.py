"""Settings DB switching contracts, using only isolated databases/config files."""
from __future__ import annotations

import json
import httpx

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import ai_profiles, akshare_apis, app_settings, db_config
from app.core import config as core_config
from app.services.async_tasks import cancel_async_task, create_async_task, get_async_task

pytestmark = pytest.mark.whitebox


@pytest.fixture
def config_client(tmp_path, monkeypatch, db_session):
    from app.services import akshare_registry
    monkeypatch.setattr(akshare_registry, "_config_cache", {})
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", False)
    akshare_registry.load_config_cache(db_session)
    config_path = tmp_path / "db-config.json"
    config_path.write_text(json.dumps({"use_mysql": False, "mysql": {}}), encoding="utf-8")
    monkeypatch.setattr(core_config, "DB_CONFIG_PATH", config_path)
    monkeypatch.setenv("AI_CONFIG_PATH", str(tmp_path / "absent-ai.json"))
    app = FastAPI()
    app.include_router(db_config.router)
    app.include_router(app_settings.router)
    app.include_router(akshare_apis.router)
    app.include_router(ai_profiles.router)
    return TestClient(app)


def test_failed_database_target_keeps_saved_configuration(config_client):
    response = config_client.put("/settings/db-config", json={
        "use_mysql": True,
        "mysql": {"host": "", "database": "", "user": "", "password": ""},
    })
    assert response.status_code == 400
    assert config_client.get("/settings/db-config").json()["use_mysql"] is False


def test_failed_schema_switch_restores_previous_runtime_database(config_client, tmp_path, monkeypatch):
    config_client.put("/settings/app/discovery.auto_data_prep_enabled", json={"value": False})
    blocked_target = tmp_path / "directory-not-a-database"
    blocked_target.mkdir()
    monkeypatch.setattr(core_config.settings, "database_url", f"sqlite:///{blocked_target.as_posix()}")
    response = config_client.put("/settings/db-config", json={"use_mysql": False, "mysql": {}})
    assert response.status_code == 500
    settings_response = config_client.get("/settings/app")
    assert settings_response.status_code == 200
    assert settings_response.json()[0]["value"] is False


def test_database_switch_is_rejected_while_a_task_is_waiting(config_client, tmp_path, monkeypatch):
    create_async_task("settings-switch-test", {})
    monkeypatch.setattr(core_config.settings, "database_url", f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    response = config_client.put("/settings/db-config", json={"use_mysql": False, "mysql": {}})
    assert response.status_code == 409


def test_successful_switch_submits_and_reads_tasks_in_the_same_database(config_client, tmp_path, monkeypatch):
    old = create_async_task("settings-switch-old", {}, use_control_plane=True)
    cancel_async_task(old.id)
    monkeypatch.setattr(core_config.settings, "database_url", f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    response = config_client.put("/settings/db-config", json={"use_mysql": False, "mysql": {}})
    assert response.status_code == 200
    new = create_async_task("settings-switch-new", {}, use_control_plane=True)
    assert get_async_task(new.id) is not None


def test_config_file_failure_restores_runtime_and_keeps_previous_saved_values(config_client, tmp_path, monkeypatch):
    config_client.put("/settings/app/discovery.auto_data_prep_enabled", json={"value": False})
    monkeypatch.setattr(core_config.settings, "database_url", f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    def cannot_replace(*args, **kwargs):
        raise PermissionError("offline read-only config target")
    monkeypatch.setattr(core_config.os, "replace", cannot_replace)
    response = config_client.put("/settings/db-config", json={"use_mysql": False, "mysql": {"host": "changed.example"}})
    assert response.status_code == 500
    assert config_client.get("/settings/db-config").json()["mysql"]["host"] == "127.0.0.1"
    assert config_client.get("/settings/app").json()[0]["value"] is False


def test_failed_switch_restores_the_original_databases_supplier_policy(config_client, tmp_path, monkeypatch):
    from app.services.akshare_utils import AkshareApiDisabledError, call_akshare_with_retry
    response = config_client.put("/external-data/apis/stock_zh_a_spot_em", json={"enabled": False, "anti_risk_strategy": "custom", "delay_min_ms": 0, "delay_max_ms": 0})
    assert response.status_code == 200
    with pytest.raises(AkshareApiDisabledError):
        call_akshare_with_retry(lambda: "supplier contacted", api_key="stock_zh_a_spot_em", max_attempts=1)
    monkeypatch.setattr(core_config.settings, "database_url", f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    def cannot_replace(*args, **kwargs):
        raise PermissionError("offline config save failure")
    monkeypatch.setattr(core_config.os, "replace", cannot_replace)
    response = config_client.put("/settings/db-config", json={"use_mysql": False, "mysql": {}})
    assert response.status_code == 500
    with pytest.raises(AkshareApiDisabledError):
        call_akshare_with_retry(lambda: "supplier contacted", api_key="stock_zh_a_spot_em", max_attempts=1)


def test_live_ai_request_blocks_database_switch(config_client, tmp_path, monkeypatch):
    profile = config_client.post("/ai/profiles", json={"name": "live-ai", "provider": "openai_compatible", "model": "offline", "base_url": "https://offline.example/v1", "auth_type": "none"}).json()
    monkeypatch.setattr(core_config.settings, "database_url", f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    statuses = []
    def provider(request):
        statuses.append(config_client.put("/settings/db-config", json={"use_mysql": False, "mysql": {}}).status_code)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    config_client.post(f"/ai/profiles/{profile['id']}/test")
    assert statuses == [409]


def test_target_policy_cache_read_failure_prevents_database_switch_commit(config_client, tmp_path, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlalchemy.exc import OperationalError
    config_client.put("/settings/app/discovery.auto_data_prep_enabled", json={"value": False})
    target_url = f"sqlite:///{(tmp_path / 'new.db').as_posix()}"
    monkeypatch.setattr(core_config.settings, "database_url", target_url)
    faults = []
    def unavailable_policy_reader(connection, cursor, statement, parameters, context, executemany):
        if str(connection.engine.url) == target_url and statement.lstrip().startswith("SELECT") and "akshare_api_config.api_key" in statement:
            faults.append(True)
            raise OperationalError(statement, parameters, Exception("offline policy reader unavailable"))
    event.listen(Engine, "before_cursor_execute", unavailable_policy_reader)
    try:
        response = config_client.put("/settings/db-config", json={"use_mysql": False, "mysql": {"host": "new.example"}})
    finally:
        event.remove(Engine, "before_cursor_execute", unavailable_policy_reader)
    assert faults
    assert response.status_code == 500
    assert config_client.get("/settings/db-config").json()["mysql"]["host"] == "127.0.0.1"
    assert config_client.get("/settings/app").json()[0]["value"] is False
