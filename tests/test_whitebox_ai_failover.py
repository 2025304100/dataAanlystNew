"""白盒测试 - WP-AI.2 多 Profile 主备降级。

覆盖：
1. AIProfile 表存在性（init_db 后表存在）
2. CRUD 服务（创建/更新/启停/删除/列表）
3. 主备降级（select_profile / failover / record_failure）
4. 缓存层（命中/未命中/TTL/清除）
5. SecretStore（环境变量/加密文件/日志不返回明文）
6. API 响应不返回明文 Secret
7. 兼容迁移（ai_config.json → AIProfile）
8. 每日请求限制
9. SQLite schema 补丁幂等性
10. MySQL 补丁函数存在性
"""
from __future__ import annotations

import inspect as _inspect
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sqla_inspect

from app.db.base import Base
from app.db.init_db import (
    _ensure_mysql_ai_profiles_table,
    _ensure_sqlite_ai_profiles_table,
)
from app.models.ai_profile import (
    AIProfile,
    HEALTH_DEGRADED,
    HEALTH_DOWN,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI_COMPATIBLE,
    PURPOSE_ALL,
    PURPOSE_CHAT,
)
from app.services import ai_profile_service
from app.services.ai_cache import AICache, get_ai_cache, reset_ai_cache
from app.services.ai_failover import (
    AIFailoverManager,
    get_failover_manager,
    reset_failover_manager,
)
from app.services.ai_profile_migration import migrate_ai_config_json
from app.services.secret_store import (
    SecretStore,
    get_secret_store,
    reset_secret_store,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """每个测试前后重置单例，避免相互污染。"""
    reset_secret_store()
    reset_ai_cache()
    reset_failover_manager()
    yield
    reset_secret_store()
    reset_ai_cache()
    reset_failover_manager()


@pytest.fixture
def secret_store(tmp_path):
    """每个测试一个独立的 SecretStore（指向临时文件）。"""
    store = SecretStore(secrets_path=tmp_path / "secrets.enc")
    yield store


def _make_profile(
    db_session,
    *,
    name="primary",
    provider=PROVIDER_OPENAI_COMPATIBLE,
    model="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    priority=0,
    is_enabled=True,
    is_fallback=False,
    purpose=PURPOSE_ALL,
    secret_value=None,
    daily_request_limit=100,
    health_status=HEALTH_UNKNOWN,
) -> AIProfile:
    return ai_profile_service.create_profile(
        db_session,
        name=name,
        provider=provider,
        model=model,
        base_url=base_url,
        auth_type="bearer" if secret_value else "none",
        secret_value=secret_value,
        priority=priority,
        is_enabled=is_enabled,
        is_fallback=is_fallback,
        purpose=purpose,
        daily_request_limit=daily_request_limit,
    )


# ----------------------------------------------------------------------------
# 1. 表存在性 + schema 补丁幂等性
# ----------------------------------------------------------------------------


def test_ai_profiles_table_exists(db_session):
    """【WP-AI.2】init_db 后 ai_profiles 表存在。"""
    engine = db_session.bind
    inspector = sqla_inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "ai_profiles" in table_names


def test_schema_patch_idempotent_sqlite(db_session):
    """【WP-AI.2】SQLite schema 补丁幂等：多次调用不报错。"""
    engine = db_session.bind
    _ensure_sqlite_ai_profiles_table(engine)
    _ensure_sqlite_ai_profiles_table(engine)  # 再次调用不应报错
    inspector = sqla_inspect(engine)
    assert inspector.has_table("ai_profiles")


def test_mysql_patch_function_exists():
    """【WP-AI.2】MySQL 补丁函数存在且签名正确。"""
    sig = _inspect.signature(_ensure_mysql_ai_profiles_table)
    params = list(sig.parameters.keys())
    assert params == ["engine"]


# ----------------------------------------------------------------------------
# 2. CRUD：创建 Profile
# ----------------------------------------------------------------------------


def test_create_profile(db_session, secret_store, monkeypatch):
    """【WP-AI.2】创建 Profile：secret_value 写入 SecretStore，DB 只存 secret_key_ref。"""
    # 让 ai_profile_service 使用本测试的 secret_store
    monkeypatch.setattr(
        "app.services.ai_profile_service.get_secret_store", lambda: secret_store
    )

    profile = ai_profile_service.create_profile(
        db_session,
        name="test-primary",
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        auth_type="bearer",
        secret_value="sk-test-secret-12345",
        priority=0,
    )
    db_session.commit()

    assert profile.id is not None
    assert profile.name == "test-primary"
    # 关键：DB 中只存 secret_key_ref，不存明文
    assert profile.secret_key_ref is not None
    assert "sk-test-secret-12345" not in (profile.secret_key_ref or "")
    # secret_value 实际存储在 SecretStore
    secret_value = secret_store.get_secret(profile.secret_key_ref)
    assert secret_value == "sk-test-secret-12345"


def test_update_profile_partial(db_session, secret_store, monkeypatch):
    """【WP-AI.2】更新 Profile：仅更新非 None 字段；更新 secret 不影响其它字段。"""
    monkeypatch.setattr(
        "app.services.ai_profile_service.get_secret_store", lambda: secret_store
    )
    profile = _make_profile(db_session, name="upd", secret_value="sk-old")
    db_session.commit()

    updated = ai_profile_service.update_profile(
        db_session, profile.id, model="gpt-4o", secret_value="sk-new"
    )
    db_session.commit()
    assert updated.model == "gpt-4o"
    # 旧 secret 已被新值覆盖
    assert secret_store.get_secret(updated.secret_key_ref) == "sk-new"


def test_enable_disable_profile(db_session):
    """【WP-AI.2】启用/禁用 Profile。"""
    profile = _make_profile(db_session, name="toggle", is_enabled=True)
    db_session.commit()
    assert ai_profile_service.disable_profile(db_session, profile.id) is True
    db_session.commit()
    db_session.refresh(profile)
    assert profile.is_enabled is False
    assert ai_profile_service.enable_profile(db_session, profile.id) is True
    db_session.commit()
    db_session.refresh(profile)
    assert profile.is_enabled is True


def test_list_profiles_ordered_by_priority(db_session):
    """【WP-AI.2】list_profiles 按 priority 升序。"""
    _make_profile(db_session, name="p2", priority=2)
    _make_profile(db_session, name="p0", priority=0)
    _make_profile(db_session, name="p1", priority=1)
    db_session.commit()
    names = [p.name for p in ai_profile_service.list_profiles(db_session)]
    assert names == ["p0", "p1", "p2"]


# ----------------------------------------------------------------------------
# 3. 主备降级
# ----------------------------------------------------------------------------


def test_select_profile_by_priority(db_session):
    """【WP-AI.2】select_profile 按优先级选择最高（priority 最小）。"""
    _make_profile(db_session, name="low", priority=10)
    _make_profile(db_session, name="high", priority=0)
    _make_profile(db_session, name="mid", priority=5)
    db_session.commit()

    mgr = get_failover_manager()
    selected = mgr.select_profile(db_session, purpose=PURPOSE_ALL)
    assert selected is not None
    assert selected.name == "high"


def test_select_profile_excludes_disabled_and_down(db_session):
    """【WP-AI.2】select_profile 排除禁用和 down 状态。"""
    p1 = _make_profile(db_session, name="disabled", priority=0, is_enabled=False)
    p2 = _make_profile(db_session, name="down", priority=1)
    p2.health_status = HEALTH_DOWN
    p3 = _make_profile(db_session, name="available", priority=2)
    db_session.commit()

    mgr = get_failover_manager()
    selected = mgr.select_profile(db_session, purpose=PURPOSE_ALL)
    assert selected is not None
    assert selected.name == "available"


def test_select_profile_purpose_filter(db_session):
    """【WP-AI.2】select_profile 按 purpose 过滤。"""
    _make_profile(db_session, name="chat-only", priority=0, purpose=PURPOSE_CHAT)
    _make_profile(db_session, name="all-purpose", priority=1, purpose=PURPOSE_ALL)
    db_session.commit()

    mgr = get_failover_manager()
    # purpose=chat 应优先选 chat-only
    selected = mgr.select_profile(db_session, purpose=PURPOSE_CHAT)
    assert selected is not None
    assert selected.name == "chat-only"
    # purpose=explanation 应只匹配 all-purpose
    selected = mgr.select_profile(db_session, purpose="explanation")
    assert selected is not None
    assert selected.name == "all-purpose"


def test_failover_to_next_profile(db_session):
    """【WP-AI.2】主备降级：主 Profile 失败后切换到备用。"""
    primary = _make_profile(db_session, name="primary", priority=0, is_fallback=False)
    fallback = _make_profile(
        db_session, name="fallback", priority=1, is_fallback=True
    )
    db_session.commit()

    mgr = get_failover_manager()
    # 主失败后切换到 fallback
    switched = mgr.failover(db_session, primary.id, purpose=PURPOSE_ALL)
    assert switched is not None
    assert switched.id == fallback.id


def test_failover_to_ollama(db_session):
    """【WP-AI.2】所有远程 Profile 不可用 → 切换到本地 Ollama。"""
    # 主 Profile：远程，已 down
    primary = _make_profile(
        db_session, name="remote-primary", priority=0,
        provider=PROVIDER_OPENAI_COMPATIBLE,
    )
    primary.health_status = HEALTH_DOWN
    # 备用 Profile：远程，已 down
    fallback = _make_profile(
        db_session, name="remote-fallback", priority=1, is_fallback=True,
        provider=PROVIDER_OPENAI_COMPATIBLE,
    )
    fallback.health_status = HEALTH_DOWN
    # 本地 Ollama
    ollama = _make_profile(
        db_session, name="local-ollama", priority=10,
        provider=PROVIDER_OLLAMA,
        base_url="http://127.0.0.1:11434",
    )
    db_session.commit()

    mgr = get_failover_manager()
    switched = mgr.failover(db_session, primary.id, purpose=PURPOSE_ALL)
    assert switched is not None
    assert switched.id == ollama.id
    assert switched.provider == PROVIDER_OLLAMA


def test_record_failure_updates_health(db_session):
    """【WP-AI.2】record_failure 更新 health_status，连续失败超阈值转为 down。"""
    profile = _make_profile(db_session, name="failable", priority=0)
    db_session.commit()

    mgr = get_failover_manager()
    mgr._reset_consecutive_failures()

    # 第一次失败：degraded
    mgr.record_failure(db_session, profile.id, error_type="timeout", error_msg="t1")
    db_session.commit()
    db_session.refresh(profile)
    assert profile.health_status == HEALTH_DEGRADED

    # 第二次失败：仍 degraded
    mgr.record_failure(db_session, profile.id, error_type="timeout", error_msg="t2")
    db_session.commit()
    db_session.refresh(profile)
    assert profile.health_status == HEALTH_DEGRADED

    # 第三次失败：达到阈值 → down
    mgr.record_failure(db_session, profile.id, error_type="timeout", error_msg="t3")
    db_session.commit()
    db_session.refresh(profile)
    assert profile.health_status == HEALTH_DOWN


def test_record_success_resets_health(db_session):
    """【WP-AI.2】record_success 重置健康状态为 healthy。"""
    profile = _make_profile(db_session, name="recovery", priority=0)
    profile.health_status = HEALTH_DEGRADED
    db_session.commit()

    mgr = get_failover_manager()
    mgr._consecutive_failures[profile.id] = 2
    mgr.record_success(db_session, profile.id, latency_ms=200, tokens=10)
    db_session.commit()
    db_session.refresh(profile)
    assert profile.health_status == HEALTH_HEALTHY
    # 连续失败计数清零
    assert profile.id not in mgr._consecutive_failures


# ----------------------------------------------------------------------------
# 4. 缓存层
# ----------------------------------------------------------------------------


def test_cache_hit():
    """【WP-AI.2】缓存命中。"""
    cache = AICache(ttl=60)
    cache.set("prompt-1", "v1", {"reply": "hello"})
    assert cache.get("prompt-1", "v1") == {"reply": "hello"}


def test_cache_miss_different_prompt():
    """【WP-AI.2】缓存未命中：不同 prompt。"""
    cache = AICache(ttl=60)
    cache.set("prompt-1", "v1", {"reply": "hello"})
    assert cache.get("prompt-2", "v1") is None


def test_cache_miss_different_version():
    """【WP-AI.2】缓存未命中：不同 context_version。"""
    cache = AICache(ttl=60)
    cache.set("prompt-1", "v1", {"reply": "hello"})
    assert cache.get("prompt-1", "v2") is None


def test_cache_ttl_expiry():
    """【WP-AI.2】缓存 TTL 过期。"""
    cache = AICache(ttl=0)  # 立即过期
    cache.set("prompt-1", "v1", {"reply": "hello"}, ttl=0)
    # ttl=0 立即过期
    assert cache.get("prompt-1", "v1") is None


def test_cache_clear():
    """【WP-AI.2】缓存清除。"""
    cache = AICache(ttl=60)
    cache.set("p1", "v1", {"a": 1})
    cache.set("p2", "v1", {"b": 2})
    assert cache.size() == 2
    cache.clear()
    assert cache.size() == 0


# ----------------------------------------------------------------------------
# 5. SecretStore
# ----------------------------------------------------------------------------


def test_secret_store_env_var(monkeypatch):
    """【WP-AI.2】SecretStore 优先从环境变量获取。"""
    monkeypatch.setenv("MY_TEST_SECRET_KEY", "env-secret-value")
    store = SecretStore()
    assert store.get_secret("MY_TEST_SECRET_KEY") == "env-secret-value"


def test_secret_store_encrypted_file(tmp_path):
    """【WP-AI.2】SecretStore 加密文件后端：set/get/list/delete。"""
    store = SecretStore(secrets_path=tmp_path / "secrets.enc")
    assert store.get_secret("k1") is None
    store.set_secret("k1", "v1")
    assert store.get_secret("k1") == "v1"
    assert "k1" in store.list_keys()
    # 删除
    assert store.delete_secret("k1") is True
    assert store.get_secret("k1") is None
    # 再次删除返回 False
    assert store.delete_secret("k1") is False


def test_secret_store_master_key_from_env(monkeypatch):
    """【WP-AI.2】master_key 从 APP_MASTER_KEY 环境变量读取。"""
    monkeypatch.setenv("APP_MASTER_KEY", "test-master-key-12345")
    store1 = SecretStore(secrets_path=Path(tempfile.gettempdir()) / "st-test1.enc")
    store1.set_secret("k1", "v1")
    # 新实例使用相同 master_key 应能解密
    store2 = SecretStore(secrets_path=Path(tempfile.gettempdir()) / "st-test1.enc")
    assert store2.get_secret("k1") == "v1"


def test_secret_store_persists_master_key_across_restart(tmp_path):
    """未配置 APP_MASTER_KEY 时，重启后仍可读取本地保存的 Secret。"""
    secrets_path = tmp_path / "secrets.enc"
    first_process = SecretStore(secrets_path=secrets_path)
    first_process.set_secret("AI_PROFILE_1_TEST", "sk-persist-after-restart")

    restarted_process = SecretStore(secrets_path=secrets_path)
    assert restarted_process.get_secret("AI_PROFILE_1_TEST") == "sk-persist-after-restart"
    assert secrets_path.with_suffix(".key").exists()


def test_secret_store_recovers_legacy_file_before_first_restart(tmp_path):
    """升级后可读取旧格式 secrets.enc，并补写持久化主密钥。"""
    secrets_path = tmp_path / "secrets.enc"
    legacy_process = SecretStore(secrets_path=secrets_path, master_key="legacy-master-key")
    legacy_process.set_secret("AI_PROFILE_1_LEGACY", "sk-legacy-value")

    upgraded_process = SecretStore(secrets_path=secrets_path)
    assert upgraded_process.get_secret("AI_PROFILE_1_LEGACY") == "sk-legacy-value"
    assert secrets_path.with_suffix(".key").exists()
    # 清理
    Path(tempfile.gettempdir(), "st-test1.enc").unlink(missing_ok=True)


def test_secret_store_never_returns_plaintext_in_logs(
    tmp_path, monkeypatch, caplog
):
    """【WP-AI.2】SecretStore 日志不返回明文 secret。"""
    monkeypatch.setenv("APP_MASTER_KEY", "test-master-key-for-logging")
    store = SecretStore(secrets_path=tmp_path / "secrets.enc")
    secret_value = "super-secret-plaintext-DO-NOT-LEAK-12345"
    store.set_secret("k_logger_test", secret_value)

    with caplog.at_level(logging.DEBUG, logger="app.services.secret_store"):
        store.get_secret("k_logger_test")
        store.list_keys()

    # 日志中不得出现明文 secret
    full_log = caplog.text
    assert secret_value not in full_log
    # 日志中可以出现 key 名
    assert "k_logger_test" in full_log or "SecretStore" in full_log


def test_secret_store_repr_does_not_leak_value(tmp_path):
    """【WP-AI.2】SecretStore.__repr__ / __str__ 不暴露 secret 值。"""
    store = SecretStore(secrets_path=tmp_path / "secrets.enc")
    store.set_secret("k1", "leak-me-if-you-can-99999")
    r = repr(store)
    s = str(store)
    assert "leak-me-if-you-can-99999" not in r
    assert "leak-me-if-you-can-99999" not in s


# ----------------------------------------------------------------------------
# 6. API 响应不返回明文 Secret
# ----------------------------------------------------------------------------


def test_api_response_no_plaintext_secret(db_session, secret_store, monkeypatch):
    """【WP-AI.2】API 响应中 secret_key_ref 只返回 key 名，不返回 secret 值。"""
    monkeypatch.setattr(
        "app.services.ai_profile_service.get_secret_store", lambda: secret_store
    )

    from app.api.router import api_router
    from app.db.session import get_db
    from fastapi import FastAPI

    app = FastAPI()
    # api_router 自身已带 prefix=/api/v1，此处不再额外加 prefix
    app.include_router(api_router)

    # 覆盖 get_db 依赖，使用测试 session
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    client = TestClient(app)
    # 创建 Profile
    resp = client.post(
        "/api/v1/ai/profiles",
        json={
            "name": "api-test",
            "provider": "openai_compatible",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "bearer",
            "secret_value": "sk-api-secret-DO-NOT-LEAK",
            "priority": 0,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 响应不应包含 secret_value 字段
    assert "secret_value" not in data
    # secret_key_ref 只返回 key 名
    assert data["secret_key_ref"] is not None
    assert "sk-api-secret-DO-NOT-LEAK" not in json.dumps(data)
    # SecretStore 中确实有值
    assert secret_store.get_secret(data["secret_key_ref"]) == "sk-api-secret-DO-NOT-LEAK"

    # 列出 Profile 也不应包含明文
    resp = client.get("/api/v1/ai/profiles")
    assert resp.status_code == 200
    text = json.dumps(resp.json())
    assert "sk-api-secret-DO-NOT-LEAK" not in text


def test_api_health_endpoint(db_session):
    """【WP-AI.2】GET /api/v1/ai/health 返回健康状态总览。"""
    _make_profile(db_session, name="h1", priority=0)
    _make_profile(db_session, name="h2", priority=1, is_fallback=True)
    db_session.commit()

    from app.api.router import api_router
    from app.db.session import get_db
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(api_router)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    client = TestClient(app)
    resp = client.get("/api/v1/ai/health")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {item["name"] for item in data}
    assert names == {"h1", "h2"}


# ----------------------------------------------------------------------------
# 7. 兼容迁移
# ----------------------------------------------------------------------------


def test_migrate_ai_config_json(db_session, secret_store, tmp_path, monkeypatch):
    """【WP-AI.2】ai_config.json 自动迁移为 AIProfile（幂等，原文件保留）。"""
    monkeypatch.setattr(
        "app.services.ai_profile_service.get_secret_store", lambda: secret_store
    )

    # 构造一个 ai_config.json
    cfg_path = tmp_path / "ai_config.json"
    cfg_data = {
        "version": 2,
        "provider": "openai_compatible",
        "service_url": "https://api.openai.com/v1",
        "api_key": "sk-migrate-test-12345",
        "model": "gpt-4o-mini",
        "enabled": True,
        "auth_type": "bearer",
        "timeout_seconds": 30,
        "max_tokens": 1024,
    }
    cfg_path.write_text(json.dumps(cfg_data), encoding="utf-8")

    # 第一次迁移
    count = migrate_ai_config_json(db_session, config_path=cfg_path)
    db_session.commit()
    assert count == 1

    # Profile 已创建
    profile = ai_profile_service.get_profile_by_name(db_session, "migrated_default")
    assert profile is not None
    assert profile.provider == PROVIDER_OPENAI_COMPATIBLE
    assert profile.model == "gpt-4o-mini"
    assert profile.base_url == "https://api.openai.com/v1"
    # secret 已迁移到 SecretStore
    assert profile.secret_key_ref is not None
    assert secret_store.get_secret(profile.secret_key_ref) == "sk-migrate-test-12345"

    # 原文件保留但已标记 migrated=true
    persisted = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert persisted.get("migrated") is True
    assert persisted.get("service_url") == "https://api.openai.com/v1"

    # 第二次迁移：幂等，不再创建
    count2 = migrate_ai_config_json(db_session, config_path=cfg_path)
    db_session.commit()
    assert count2 == 0
    # 仍只有一条 migrated_default
    profiles = ai_profile_service.list_profiles(db_session)
    migrated = [p for p in profiles if p.name == "migrated_default"]
    assert len(migrated) == 1


def test_migrate_ai_config_json_skips_when_missing(db_session, tmp_path):
    """【WP-AI.2】ai_config.json 不存在时跳过迁移，返回 0。"""
    count = migrate_ai_config_json(
        db_session, config_path=tmp_path / "nonexistent.json"
    )
    assert count == 0


# ----------------------------------------------------------------------------
# 8. 每日请求限制
# ----------------------------------------------------------------------------


def test_daily_request_limit_blocks_select(db_session):
    """【WP-AI.2】当日请求耗尽时 select_profile 跳过该 Profile。"""
    # 主 Profile：配额已耗尽
    p1 = _make_profile(db_session, name="exhausted", priority=0, daily_request_limit=2)
    p1.daily_request_count = 2
    p1.daily_request_reset_at = _utcnow_naive()  # 今天，未过 24h
    # 备用 Profile：仍有配额
    p2 = _make_profile(db_session, name="available", priority=1, daily_request_limit=100)
    db_session.commit()

    mgr = get_failover_manager()
    selected = mgr.select_profile(db_session, purpose=PURPOSE_ALL)
    assert selected is not None
    assert selected.name == "available"


def test_daily_request_limit_resets_after_24h(db_session):
    """【WP-AI.2】超过 24h 后配额自动重置，select_profile 不再跳过。"""
    p1 = _make_profile(db_session, name="stale", priority=0, daily_request_limit=2)
    p1.daily_request_count = 2
    p1.daily_request_reset_at = _utcnow_naive() - timedelta(hours=25)
    db_session.commit()

    mgr = get_failover_manager()
    selected = mgr.select_profile(db_session, purpose=PURPOSE_ALL)
    assert selected is not None
    assert selected.name == "stale"


def test_increment_daily_count(db_session):
    """【WP-AI.2】increment_daily_count 累加计数并在配额内返回 True。"""
    p = _make_profile(db_session, name="counter", priority=0, daily_request_limit=3)
    db_session.commit()

    assert ai_profile_service.increment_daily_count(db_session, p.id) is True
    db_session.commit()
    db_session.refresh(p)
    assert p.daily_request_count == 1

    assert ai_profile_service.increment_daily_count(db_session, p.id) is True
    assert ai_profile_service.increment_daily_count(db_session, p.id) is True
    db_session.commit()
    db_session.refresh(p)
    assert p.daily_request_count == 3
    # 第 4 次超出配额
    assert ai_profile_service.increment_daily_count(db_session, p.id) is False
    db_session.commit()
    db_session.refresh(p)
    assert p.daily_request_count == 4  # 计数仍累加，但返回 False


def test_reset_daily_counters(db_session):
    """【WP-AI.2】reset_daily_counters 重置所有 Profile 的计数器。"""
    p1 = _make_profile(db_session, name="r1", priority=0, daily_request_limit=10)
    p2 = _make_profile(db_session, name="r2", priority=1, daily_request_limit=10)
    p1.daily_request_count = 5
    p2.daily_request_count = 8
    db_session.commit()

    count = ai_profile_service.reset_daily_counters(db_session)
    db_session.commit()
    db_session.refresh(p1)
    db_session.refresh(p2)
    assert count == 2
    assert p1.daily_request_count == 0
    assert p2.daily_request_count == 0
    assert p1.daily_request_reset_at is not None
    assert p2.daily_request_reset_at is not None


# ----------------------------------------------------------------------------
# 9. 字段完整性 + 不返回明文 Secret
# ----------------------------------------------------------------------------


def test_ai_profile_repr_does_not_leak_secret(db_session, secret_store, monkeypatch):
    """【WP-AI.2】AIProfile.__repr__ 不暴露 secret 值。"""
    monkeypatch.setattr(
        "app.services.ai_profile_service.get_secret_store", lambda: secret_store
    )
    profile = _make_profile(
        db_session, name="repr-test", secret_value="sk-repr-leak-99999"
    )
    db_session.commit()
    r = repr(profile)
    assert "sk-repr-leak-99999" not in r
    # secret_key_ref 字段名可以出现，但值不能是明文 secret
    # （secret_key_ref 形如 AI_PROFILE_1_REPR_TEST，不是 sk-... 开头）


def test_get_health_status(db_session):
    """【WP-AI.2】get_health_status 返回所有 Profile 健康状态。"""
    p1 = _make_profile(db_session, name="hs1", priority=0)
    p2 = _make_profile(db_session, name="hs2", priority=1)
    p1.health_status = HEALTH_HEALTHY
    p2.health_status = HEALTH_DOWN
    db_session.commit()

    mgr = get_failover_manager()
    statuses = mgr.get_health_status(db_session)
    assert len(statuses) == 2
    by_name = {s["name"]: s for s in statuses}
    assert by_name["hs1"]["health_status"] == HEALTH_HEALTHY
    assert by_name["hs2"]["health_status"] == HEALTH_DOWN
