"""AI 主备降级管理器（WP-AI.2 多 Profile 主备降级）。

负责 AI Profile 的选择、健康状态跟踪、主备降级。

降级逻辑：
- 主 Profile 超时/限流/429 → 标记 degraded
- 切换到下一个可用 Profile（is_fallback=True 或优先级次高）
- 如果所有远程 Profile 不可用 → 切换到本地 Ollama（若配置）
- 切换在回复的 provider_used 字段中显示
- AI 失败不阻塞任何业务流程（try/except 包裹，返回 None 或默认消息）

project_memory 硬约束：
- AI 失败不阻塞扫描/回测/告警/交易
- 切换在回复中显示 provider_used 字段
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ai_profile import (
    AIProfile,
    HEALTH_DEGRADED,
    HEALTH_DOWN,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    PROVIDER_OLLAMA,
    PURPOSE_ALL,
)
from app.services import ai_profile_service

logger = logging.getLogger(__name__)


# 失败原因 → 健康状态映射
_FAILURE_TO_HEALTH = {
    "timeout": HEALTH_DEGRADED,
    "rate_limit": HEALTH_DEGRADED,  # 429
    "429": HEALTH_DEGRADED,
    "connection": HEALTH_DOWN,
    "auth": HEALTH_DOWN,
    "401": HEALTH_DOWN,
    "403": HEALTH_DOWN,
    "server_error": HEALTH_DEGRADED,
    "500": HEALTH_DEGRADED,
    "502": HEALTH_DEGRADED,
    "503": HEALTH_DEGRADED,
    "504": HEALTH_DEGRADED,
}

# 连续失败多少次后转为 down
_DEGRADED_TO_DOWN_THRESHOLD = 3


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AIFailoverManager:
    """AI 主备降级管理（WP-AI.2）。

    无状态单例，所有状态持久化到 DB（AIProfile.health_status 等字段）。
    """

    def __init__(self) -> None:
        # 内存中的连续失败计数（profile_id -> count），不持久化
        self._consecutive_failures: dict[int, int] = {}

    # ── Profile 选择 ─────────────────────────────────────────

    def select_profile(
        self,
        db: Session,
        purpose: str = PURPOSE_ALL,
    ) -> AIProfile | None:
        """选择可用的 Profile（按优先级）。

        策略：
        1. 过滤：is_enabled=True AND health_status != 'down'
        2. purpose 匹配：purpose='all' 的 Profile 适配所有用途，否则精确匹配
        3. 检查 daily_request_limit：剩余配额 > 0
        4. 按 priority 升序、is_fallback=False 优先
        5. 返回第一个可用 Profile，None 表示全部不可用

        AI 失败不阻塞业务流程：返回 None 时上层应 try/except 并使用默认消息。
        """
        try:
            stmt = select(AIProfile).where(
                AIProfile.is_enabled.is_(True),
                AIProfile.health_status != HEALTH_DOWN,
            )
            profiles = list(db.execute(stmt).scalars().all())
        except Exception as exc:
            logger.warning("select_profile DB query failed: %s", exc)
            return None

        if not profiles:
            return None

        now = _utcnow_naive()
        candidates: list[AIProfile] = []
        for p in profiles:
            # purpose 匹配
            if p.purpose != PURPOSE_ALL and p.purpose != purpose:
                continue
            # 检查日配额
            if p.daily_request_reset_at is not None:
                # 超过 24h 视为已重置，配额恢复
                if (now - p.daily_request_reset_at).total_seconds() >= 86400:
                    pass  # 配额已重置，可用
                elif p.daily_request_count >= p.daily_request_limit:
                    continue  # 配额已耗尽
            else:
                pass  # 未设置 reset_at，视为可用
            candidates.append(p)

        if not candidates:
            return None

        # 排序：priority 升序，is_fallback=False 优先（False 排前）
        candidates.sort(key=lambda p: (p.priority, p.is_fallback, p.id))
        return candidates[0]

    # ── 健康记录 ─────────────────────────────────────────────

    def record_success(
        self,
        db: Session,
        profile_id: int,
        latency_ms: int = 0,
        tokens: int = 0,
    ) -> None:
        """记录成功：重置连续失败计数，标记 healthy。"""
        try:
            profile = db.get(AIProfile, profile_id)
            if profile is None:
                return
            profile.health_status = HEALTH_HEALTHY
            profile.last_health_check = _utcnow_naive()
            # 增加当日请求计数
            ai_profile_service.increment_daily_count(db, profile_id)
            db.flush()
        except Exception as exc:
            logger.warning("record_success failed for profile %s: %s", profile_id, exc)
        finally:
            self._consecutive_failures.pop(profile_id, None)

    def record_failure(
        self,
        db: Session,
        profile_id: int,
        error_type: str,
        error_msg: str = "",
    ) -> None:
        """记录失败：更新 health_status，连续失败超阈值转为 down。

        error_type: timeout/rate_limit/429/connection/auth/401/403/
                    server_error/500/502/503/504/unknown
        """
        try:
            profile = db.get(AIProfile, profile_id)
            if profile is None:
                return
            new_status = _FAILURE_TO_HEALTH.get(error_type, HEALTH_DEGRADED)
            count = self._consecutive_failures.get(profile_id, 0) + 1
            self._consecutive_failures[profile_id] = count
            # 连续失败超阈值 → down
            if new_status == HEALTH_DEGRADED and count >= _DEGRADED_TO_DOWN_THRESHOLD:
                new_status = HEALTH_DOWN
            profile.health_status = new_status
            profile.last_health_check = _utcnow_naive()
            db.flush()
            logger.warning(
                "AIProfile %s failure: type=%s count=%d new_status=%s msg=%s",
                profile_id, error_type, count, new_status, error_msg[:120],
            )
        except Exception as exc:
            logger.warning("record_failure failed for profile %s: %s", profile_id, exc)

    # ── 主备切换 ─────────────────────────────────────────────

    def failover(
        self,
        db: Session,
        failed_profile_id: int,
        purpose: str = PURPOSE_ALL,
    ) -> AIProfile | None:
        """切换到备用 Profile。

        顺序：
        1. 显式备用 Profile（is_fallback=True，按 priority 升序）
        2. 优先级次高的可用 Profile
        3. 本地 Ollama Profile（provider=ollama，若配置且可用）

        返回 None 表示全部不可用，上层应 try/except 并使用默认消息。
        """
        try:
            # 优先：is_fallback=True 的可用 Profile
            stmt = select(AIProfile).where(
                AIProfile.is_enabled.is_(True),
                AIProfile.health_status != HEALTH_DOWN,
                AIProfile.id != failed_profile_id,
                AIProfile.is_fallback.is_(True),
            )
            if purpose != PURPOSE_ALL:
                stmt = stmt.where(
                    (AIProfile.purpose == purpose) | (AIProfile.purpose == PURPOSE_ALL)
                )
            stmt = stmt.order_by(AIProfile.priority.asc(), AIProfile.id.asc())
            fallback = db.execute(stmt).scalars().first()
            if fallback is not None:
                logger.info(
                    "Failover: profile %s → fallback profile %s",
                    failed_profile_id, fallback.id,
                )
                return fallback

            # 次优：优先级次高的可用 Profile（不限 is_fallback）
            stmt = select(AIProfile).where(
                AIProfile.is_enabled.is_(True),
                AIProfile.health_status != HEALTH_DOWN,
                AIProfile.id != failed_profile_id,
            )
            if purpose != PURPOSE_ALL:
                stmt = stmt.where(
                    (AIProfile.purpose == purpose) | (AIProfile.purpose == PURPOSE_ALL)
                )
            stmt = stmt.order_by(AIProfile.priority.asc(), AIProfile.id.asc())
            next_profile = db.execute(stmt).scalars().first()
            if next_profile is not None:
                logger.info(
                    "Failover: profile %s → next profile %s",
                    failed_profile_id, next_profile.id,
                )
                return next_profile

            # 最后：本地 Ollama
            stmt = select(AIProfile).where(
                AIProfile.is_enabled.is_(True),
                AIProfile.provider == PROVIDER_OLLAMA,
                AIProfile.id != failed_profile_id,
            )
            stmt = stmt.order_by(AIProfile.priority.asc(), AIProfile.id.asc())
            ollama = db.execute(stmt).scalars().first()
            if ollama is not None:
                logger.info(
                    "Failover: profile %s → local Ollama profile %s",
                    failed_profile_id, ollama.id,
                )
                return ollama

            logger.warning(
                "Failover: no available profile after %s failed", failed_profile_id
            )
            return None
        except Exception as exc:
            logger.warning("failover failed for profile %s: %s", failed_profile_id, exc)
            return None

    # ── 健康总览 ─────────────────────────────────────────────

    def get_health_status(self, db: Session) -> list[dict]:
        """获取所有 Profile 健康状态。"""
        try:
            stmt = select(AIProfile).order_by(AIProfile.priority.asc(), AIProfile.id.asc())
            result: list[dict] = []
            for p in db.execute(stmt).scalars().all():
                result.append({
                    "id": p.id,
                    "name": p.name,
                    "provider": p.provider,
                    "model": p.model,
                    "priority": p.priority,
                    "is_enabled": p.is_enabled,
                    "is_fallback": p.is_fallback,
                    "health_status": p.health_status,
                    "last_health_check": p.last_health_check.isoformat()
                    if p.last_health_check
                    else None,
                    "daily_request_count": p.daily_request_count,
                    "daily_request_limit": p.daily_request_limit,
                })
            return result
        except Exception as exc:
            logger.warning("get_health_status failed: %s", exc)
            return []

    # ── 重置（测试用） ───────────────────────────────────────

    def _reset_consecutive_failures(self) -> None:
        """重置内存中的连续失败计数（仅测试用）。"""
        self._consecutive_failures.clear()


# ── 单例 ────────────────────────────────────────────────────

_singleton: AIFailoverManager | None = None
_singleton_lock = None


def _get_lock():
    global _singleton_lock
    import threading
    if _singleton_lock is None:
        _singleton_lock = threading.Lock()
    return _singleton_lock


def get_failover_manager() -> AIFailoverManager:
    """获取全局 AIFailoverManager 单例。"""
    global _singleton
    if _singleton is None:
        with _get_lock():
            if _singleton is None:
                _singleton = AIFailoverManager()
    return _singleton


def reset_failover_manager() -> None:
    """重置单例（仅用于测试）。"""
    global _singleton
    with _get_lock():
        _singleton = None


__all__ = [
    "AIFailoverManager",
    "get_failover_manager",
    "reset_failover_manager",
]
