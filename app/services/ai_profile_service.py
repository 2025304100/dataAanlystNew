"""AI Profile 管理服务（WP-AI.2 多 Profile 主备降级）。

提供 AIProfile 的 CRUD、启停、连接测试、模型发现、用量查询等能力。

设计要点：
- 鉴权字段不存明文：secret_value 通过 SecretStore 写入，AIProfile 只存 secret_key_ref
- 测试连接 / 发现模型使用 httpx 同步请求，超时由 Profile.timeout_seconds 控制
- 测试连接 / 发现模型失败时不抛异常，返回 {success: False, error: ...}
- AI 失败不阻塞业务流程（上层 try/except 包裹）

project_memory 硬约束：
- 永不返回明文 Secret（API 响应只返回 secret_key_ref 名称）
- AI 失败不阻塞任何业务流程
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ai_profile import (
    AIProfile,
    AUTH_BEARER,
    AUTH_NONE,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    PURPOSE_ALL,
)
from app.services.secret_store import SecretStore, get_secret_store

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """返回无时区信息的 UTC 当前时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── CRUD ────────────────────────────────────────────────────


def list_profiles(db: Session) -> list[AIProfile]:
    """列出所有 Profile（按 priority 升序、id 升序）。"""
    stmt = select(AIProfile).order_by(AIProfile.priority.asc(), AIProfile.id.asc())
    return list(db.execute(stmt).scalars().all())


def get_profile(db: Session, profile_id: int) -> AIProfile | None:
    """根据 ID 获取 Profile。"""
    return db.get(AIProfile, profile_id)


def get_profile_by_name(db: Session, name: str) -> AIProfile | None:
    """根据名称获取 Profile。"""
    stmt = select(AIProfile).where(AIProfile.name == name)
    return db.execute(stmt).scalar_one_or_none()


def create_profile(
    db: Session,
    *,
    name: str,
    provider: str,
    model: str,
    base_url: str | None = None,
    auth_type: str | None = None,
    secret_value: str | None = None,
    timeout_seconds: int = 30,
    max_tokens: int = 4096,
    max_context_tokens: int = 8192,
    daily_request_limit: int = 100,
    max_concurrent: int = 3,
    purpose: str = PURPOSE_ALL,
    priority: int = 0,
    is_enabled: bool = True,
    is_fallback: bool = False,
) -> AIProfile:
    """创建 Profile。

    secret_value 不直接存储，而是写入 SecretStore，AIProfile 只存 secret_key_ref。
    secret_key_ref 命名规则：AI_PROFILE_{ID}_{NAME_UPPER}，ID 创建后回填。
    """
    if not name or not provider or not model:
        raise ValueError("name, provider, model 不能为空")

    profile = AIProfile(
        name=name,
        provider=provider,
        base_url=base_url,
        model=model,
        auth_type=auth_type,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        max_context_tokens=max_context_tokens,
        daily_request_limit=daily_request_limit,
        max_concurrent=max_concurrent,
        purpose=purpose,
        priority=priority,
        is_enabled=is_enabled,
        is_fallback=is_fallback,
        health_status=HEALTH_UNKNOWN,
    )
    db.add(profile)
    db.flush()  # 获取 id

    # 写入 secret（若提供）
    if secret_value:
        secret_key_ref = f"AI_PROFILE_{profile.id}_{name.upper().replace('-', '_')}"
        store = get_secret_store()
        store.set_secret(secret_key_ref, secret_value)
        profile.secret_key_ref = secret_key_ref

    db.flush()
    logger.info(
        "AIProfile created: id=%s name=%s provider=%s",
        profile.id, profile.name, profile.provider,
    )
    return profile


def update_profile(
    db: Session,
    profile_id: int,
    *,
    name: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    auth_type: str | None = None,
    secret_value: str | None = None,
    timeout_seconds: int | None = None,
    max_tokens: int | None = None,
    max_context_tokens: int | None = None,
    daily_request_limit: int | None = None,
    max_concurrent: int | None = None,
    purpose: str | None = None,
    priority: int | None = None,
    is_enabled: bool | None = None,
    is_fallback: bool | None = None,
) -> AIProfile | None:
    """更新 Profile。仅更新非 None 字段。"""
    profile = db.get(AIProfile, profile_id)
    if profile is None:
        return None

    if name is not None:
        profile.name = name
    if provider is not None:
        profile.provider = provider
    if base_url is not None:
        profile.base_url = base_url
    if model is not None:
        profile.model = model
    if auth_type is not None:
        profile.auth_type = auth_type
    if timeout_seconds is not None:
        profile.timeout_seconds = timeout_seconds
    if max_tokens is not None:
        profile.max_tokens = max_tokens
    if max_context_tokens is not None:
        profile.max_context_tokens = max_context_tokens
    if daily_request_limit is not None:
        profile.daily_request_limit = daily_request_limit
    if max_concurrent is not None:
        profile.max_concurrent = max_concurrent
    if purpose is not None:
        profile.purpose = purpose
    if priority is not None:
        profile.priority = priority
    if is_enabled is not None:
        profile.is_enabled = is_enabled
    if is_fallback is not None:
        profile.is_fallback = is_fallback

    # 更新 secret（若提供）
    if secret_value is not None:
        secret_key_ref = (
            profile.secret_key_ref
            or f"AI_PROFILE_{profile.id}_{profile.name.upper().replace('-', '_')}"
        )
        store = get_secret_store()
        store.set_secret(secret_key_ref, secret_value)
        profile.secret_key_ref = secret_key_ref

    db.flush()
    logger.info("AIProfile updated: id=%s", profile_id)
    return profile


def enable_profile(db: Session, profile_id: int) -> bool:
    """启用 Profile。"""
    profile = db.get(AIProfile, profile_id)
    if profile is None:
        return False
    profile.is_enabled = True
    db.flush()
    return True


def disable_profile(db: Session, profile_id: int) -> bool:
    """禁用 Profile。"""
    profile = db.get(AIProfile, profile_id)
    if profile is None:
        return False
    profile.is_enabled = False
    db.flush()
    return True


def delete_profile(db: Session, profile_id: int) -> bool:
    """删除 Profile（同时删除 SecretStore 中的 secret）。"""
    profile = db.get(AIProfile, profile_id)
    if profile is None:
        return False
    # 删除 secret
    if profile.secret_key_ref:
        try:
            store = get_secret_store()
            store.delete_secret(profile.secret_key_ref)
        except Exception as exc:
            logger.warning(
                "Failed to delete secret for profile %s: %s", profile_id, exc
            )
    db.delete(profile)
    db.flush()
    return True


# ── 连接测试 / 模型发现 ─────────────────────────────────────


def _resolve_secret(profile: AIProfile) -> str:
    """从 SecretStore 解析 Profile 的 secret 值。"""
    if not profile.secret_key_ref:
        return ""
    store = get_secret_store()
    value = store.get_secret(profile.secret_key_ref)
    return value or ""


def _auth_headers(profile: AIProfile, *, json_content: bool = False) -> dict[str, str]:
    """根据 Profile 构建鉴权 headers。"""
    headers: dict[str, str] = {}
    api_key = _resolve_secret(profile)
    auth_type = profile.auth_type or AUTH_BEARER
    if auth_type == AUTH_NONE or not api_key:
        pass
    elif auth_type == AUTH_BEARER:
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_type == "api_key":
        headers["x-api-key"] = api_key
    elif auth_type == "api-key":
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    if profile.provider == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")
    if json_content:
        headers.setdefault("Content-Type", "application/json")
    return headers


def _endpoint_url(profile: AIProfile, path: str = "/chat/completions") -> str:
    """拼接完整 endpoint URL。"""
    if not profile.base_url:
        return path
    base = profile.base_url.rstrip("/")
    if path.startswith(("http://", "https://")):
        return path
    return f"{base}/{path.lstrip('/')}"


def test_connection(db: Session, profile_id: int) -> dict:
    """测试连接，返回 {success, latency_ms, model_info, error}。

    AI 失败不抛异常，返回 {success: False, error: ...}。
    """
    profile = db.get(AIProfile, profile_id)
    if profile is None:
        return {"success": False, "error": "Profile 不存在", "latency_ms": 0}
    if not profile.base_url:
        return {"success": False, "error": "未配置 base_url", "latency_ms": 0}

    started = time.time()
    try:
        headers = _auth_headers(profile, json_content=True)
        # 简单测试：发送 "Reply with OK." 单条消息
        payload = {
            "model": profile.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "temperature": 0,
        }
        endpoint = _endpoint_url(profile, "/chat/completions")
        # Profile 测试：至少给 60s 兜底
        base_timeout = max(float(profile.timeout_seconds), 60.0)
        with httpx.Client(timeout=base_timeout) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
        latency_ms = int((time.time() - started) * 1000)
        if resp.status_code >= 400:
            return {
                "success": False,
                "latency_ms": latency_ms,
                "error": f"HTTP {resp.status_code}: {resp.text[:160]}",
                "model_info": None,
            }
        # 更新健康状态
        profile.health_status = HEALTH_HEALTHY
        profile.last_health_check = _utcnow_naive()
        db.flush()
        return {
            "success": True,
            "latency_ms": latency_ms,
            "model_info": {"model": profile.model, "provider": profile.provider},
            "error": None,
        }
    except httpx.TimeoutException:
        latency_ms = int((time.time() - started) * 1000)
        return {"success": False, "latency_ms": latency_ms, "error": "请求超时", "model_info": None}
    except httpx.RequestError as exc:
        latency_ms = int((time.time() - started) * 1000)
        logger.warning("AI connection test failed for profile %s: %s", profile_id, exc)
        return {
            "success": False,
            "latency_ms": latency_ms,
            "error": f"连接失败：{str(exc)[:120]}",
            "model_info": None,
        }
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        logger.exception("AI connection test error for profile %s", profile_id)
        return {
            "success": False,
            "latency_ms": latency_ms,
            "error": f"内部错误：{str(exc)[:120]}",
            "model_info": None,
        }


def discover_models(db: Session, profile_id: int) -> list[dict]:
    """发现可用模型，返回模型对象列表 [{id, owned_by}]。

    AI 失败不抛异常，返回空列表。
    """
    profile = db.get(AIProfile, profile_id)
    if profile is None:
        return []
    if not profile.base_url:
        return []
    try:
        endpoint = _endpoint_url(profile, "/models")
        # 发现模型：至少给 60s 兜底
        base_timeout = max(float(profile.timeout_seconds), 60.0)
        with httpx.Client(timeout=base_timeout) as client:
            resp = client.get(endpoint, headers=_auth_headers(profile))
        if resp.status_code >= 400:
            logger.warning(
                "discover_models HTTP %s for profile %s",
                resp.status_code, profile_id,
            )
            return []
        data = resp.json()
        # 兼容 OpenAI / Ollama 格式
        if isinstance(data, list):
            raw_models = data
        elif isinstance(data, dict):
            raw_models = data.get("data") or data.get("models") or []
        else:
            raw_models = []
        models: list[dict] = []
        for item in raw_models:
            if isinstance(item, str):
                models.append({"id": item})
            elif isinstance(item, dict):
                mid = item.get("id") or item.get("name") or item.get("model") or ""
                owned_by = item.get("owned_by") or item.get("provider") or None
                if mid:
                    entry: dict = {"id": str(mid)}
                    if owned_by:
                        entry["owned_by"] = str(owned_by)
                    models.append(entry)
        return models
    except httpx.TimeoutException:
        logger.warning("discover_models timeout for profile %s", profile_id)
        return []
    except Exception as exc:
        logger.warning("discover_models failed for profile %s: %s", profile_id, exc)
        return []


# ── 用量与计数 ──────────────────────────────────────────────


def get_daily_usage(db: Session, profile_id: int) -> dict:
    """获取当日用量。"""
    profile = db.get(AIProfile, profile_id)
    if profile is None:
        return {}
    return {
        "profile_id": profile.id,
        "daily_request_count": profile.daily_request_count,
        "daily_request_limit": profile.daily_request_limit,
        "remaining": max(0, profile.daily_request_limit - profile.daily_request_count),
        "daily_request_reset_at": profile.daily_request_reset_at.isoformat()
        if profile.daily_request_reset_at
        else None,
    }


def increment_daily_count(db: Session, profile_id: int) -> bool:
    """增加当日请求计数。返回是否仍在配额内。"""
    profile = db.get(AIProfile, profile_id)
    if profile is None:
        return False
    # 检查是否需要重置（超过 24h）
    now = _utcnow_naive()
    if (
        profile.daily_request_reset_at is None
        or (now - profile.daily_request_reset_at).total_seconds() >= 86400
    ):
        profile.daily_request_count = 0
        profile.daily_request_reset_at = now
    profile.daily_request_count += 1
    db.flush()
    return profile.daily_request_count <= profile.daily_request_limit


def reset_daily_counters(db: Session) -> int:
    """每日重置所有 Profile 的计数器（由调度器调用）。返回重置的 Profile 数。"""
    stmt = select(AIProfile)
    count = 0
    now = _utcnow_naive()
    for profile in db.execute(stmt).scalars().all():
        profile.daily_request_count = 0
        profile.daily_request_reset_at = now
        count += 1
    db.flush()
    logger.info("Reset daily counters for %d AI profiles", count)
    return count


__all__ = [
    "list_profiles",
    "get_profile",
    "get_profile_by_name",
    "create_profile",
    "update_profile",
    "enable_profile",
    "disable_profile",
    "delete_profile",
    "test_connection",
    "discover_models",
    "get_daily_usage",
    "increment_daily_count",
    "reset_daily_counters",
]
