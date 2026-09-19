"""AI Profile 兼容迁移（WP-AI.2）。

将单一 `ai_config.json` 兼容迁移为 AIProfile 记录。

迁移规则：
- 检查是否存在 ai_config.json
- 若存在且未迁移：自动迁移为 AIProfile 记录
- 迁移后原文件保留（不删除），但标记为已迁移（添加 migrated=true 字段）
- 迁移幂等：多次调用只迁移一次

project_memory 硬约束：
- 不删除原文件
- 双库迁移幂等
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ai_profile import (
    AIProfile,
    AUTH_BEARER,
    AUTH_NONE,
    HEALTH_UNKNOWN,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_OLLAMA,
    PROVIDER_ANTHROPIC,
    PURPOSE_ALL,
)
from app.services import ai_profile_service
from app.services.secret_store import get_secret_store

logger = logging.getLogger(__name__)


# 默认 ai_config.json 路径（与 app.api.routes.ai_config.AI_CONFIG_PATH 一致）
def _default_ai_config_path() -> Path:
    base_dir = Path(__file__).resolve().parents[2]
    return Path(
        os.getenv(
            "AI_CONFIG_PATH",
            str(base_dir / "config" / "ai_config.json"),
        )
    ).expanduser().resolve()


def migrate_ai_config_json(
    db: Session,
    config_path: Path | str | None = None,
) -> int:
    """将 ai_config.json 迁移为 AIProfile 记录。

    幂等：若 ai_config.json 已标记 migrated=true，或同名 Profile 已存在，则跳过。

    Returns:
        迁移的 Profile 数（0 表示无需迁移）
    """
    path = Path(config_path) if config_path else _default_ai_config_path()
    if not path.exists():
        logger.info("ai_config.json not found at %s, skip migration", path)
        return 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read ai_config.json: %s", exc)
        return 0

    if not isinstance(data, dict):
        logger.warning("ai_config.json root must be object, skip migration")
        return 0

    # 已迁移标记
    if data.get("migrated") is True:
        logger.debug("ai_config.json already migrated, skip")
        return 0

    # 检查同名 Profile 是否已存在（幂等保护）
    name = "migrated_default"
    existing = ai_profile_service.get_profile_by_name(db, name)
    if existing is not None:
        # 已存在，标记原文件为已迁移
        _mark_migrated(path, data)
        return 0

    # 提取字段（与 _DEFAULT_AI_CONFIG 对齐）
    provider_raw = str(data.get("provider") or PROVIDER_OPENAI_COMPATIBLE)
    # 把 ai_config 的 provider 值映射到 AIProfile.provider
    provider_map = {
        "openai_compatible": PROVIDER_OPENAI_COMPATIBLE,
        "anthropic": PROVIDER_ANTHROPIC,
        "ollama": PROVIDER_OLLAMA,
        "custom": PROVIDER_OPENAI_COMPATIBLE,
    }
    provider = provider_map.get(provider_raw, PROVIDER_OPENAI_COMPATIBLE)

    base_url = str(data.get("service_url") or "").strip().rstrip("/") or None
    model = str(data.get("model") or "").strip() or "gpt-4o-mini"

    auth_type_raw = str(data.get("auth_type") or AUTH_BEARER)
    # 把 ai_config 的 auth_type 值映射到 AIProfile.auth_type
    auth_type_map = {
        "bearer": AUTH_BEARER,
        "x-api-key": "api_key",
        "api-key": "api_key",
        "api_key": "api_key",
        "custom": AUTH_BEARER,
        "none": AUTH_NONE,
    }
    auth_type = auth_type_map.get(auth_type_raw, AUTH_BEARER)

    api_key = str(data.get("api_key") or "").strip()
    enabled = bool(data.get("enabled"))
    timeout_seconds = int(data.get("timeout_seconds") or 30)
    max_tokens = int(data.get("max_tokens") or 4096)

    # 创建 Profile
    try:
        profile = ai_profile_service.create_profile(
            db,
            name=name,
            provider=provider,
            model=model,
            base_url=base_url,
            auth_type=auth_type if api_key else AUTH_NONE,
            secret_value=api_key if api_key else None,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            purpose=PURPOSE_ALL,
            priority=0,
            is_enabled=enabled,
            is_fallback=False,
        )
        # 标记 health_status
        profile.health_status = HEALTH_UNKNOWN
        db.flush()
    except Exception as exc:
        logger.warning("Failed to create migrated AIProfile: %s", exc)
        return 0

    # 标记原文件为已迁移（不删除）
    _mark_migrated(path, data)
    logger.info(
        "Migrated ai_config.json to AIProfile id=%s name=%s", profile.id, profile.name
    )
    return 1


def _mark_migrated(path: Path, data: dict) -> None:
    """在原 ai_config.json 中添加 migrated=true 标记。"""
    try:
        data["migrated"] = True
        data["migrated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Failed to mark ai_config.json as migrated: %s", exc)


__all__ = [
    "migrate_ai_config_json",
]
