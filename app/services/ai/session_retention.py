"""AI 会话保留期管理（WP-AI.6 结构化输出与审计）。

提供会话保留期配置读取、过期会话清理、永久删除能力。

设计要点：
- 保留期可配置（默认 90 天，通过 AI_SESSION_RETENTION_DAYS 环境变量覆盖）
- cleanup_expired_sessions：软删除（active → archived），返回清理数量
- delete_session_permanently：永久删除（物理删除会话 + 消息 + 审计记录）
- 用户可主动删除会话（满足 GDPR/隐私合规要求）

project_memory 硬约束：
- 会话保留期可配置，用户可删除
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_session import (
    AIActionAudit,
    AIMessage,
    AISession,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    SESSION_STATUS_DELETED,
)

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """当前 UTC 时间（naive，与项目其他模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_retention_days() -> int:
    """获取会话保留天数（从配置读取，默认 90 天）。

    Returns:
        保留天数（最小 1）
    """
    days = int(getattr(settings, "AI_SESSION_RETENTION_DAYS", 90))
    return max(1, days)


def cleanup_expired_sessions(db: Session) -> int:
    """清理过期会话（软删除）（WP-AI.6）。

    将超过保留期的 active 会话标记为 archived。
    不删除已 archived/deleted 的会话（保留历史可追溯）。

    Returns:
        本次清理（归档）的会话数量
    """
    retention_days = get_retention_days()
    cutoff = _utcnow_naive() - timedelta(days=retention_days)

    try:
        # 查询需要归档的过期 active 会话
        stmt = (
            select(AISession)
            .where(
                and_(
                    AISession.status == SESSION_STATUS_ACTIVE,
                    AISession.created_at < cutoff,
                )
            )
        )
        expired_sessions = list(db.execute(stmt).scalars().all())

        if not expired_sessions:
            return 0

        # 批量更新为 archived
        expired_ids = [s.id for s in expired_sessions]
        update_stmt = (
            update(AISession)
            .where(
                and_(
                    AISession.id.in_(expired_ids),
                    AISession.status == SESSION_STATUS_ACTIVE,
                )
            )
            .values(status=SESSION_STATUS_ARCHIVED)
        )
        result = db.execute(update_stmt)
        db.commit()

        count = int(result.rowcount or 0)
        logger.info(
            "cleanup_expired_sessions: archived %s sessions (retention=%s days, cutoff=%s)",
            count, retention_days, cutoff.isoformat(),
        )
        return count
    except Exception as exc:
        logger.warning("cleanup_expired_sessions failed: %s", exc)
        db.rollback()
        return 0


def delete_session_permanently(db: Session, session_id: int) -> bool:
    """永久删除会话（用户主动删除）（WP-AI.6）。

    物理删除会话及其所有消息和审计记录。
    与 ai_session_service.delete_session（软删除）不同，此方法不可恢复。

    Args:
        db: 数据库会话
        session_id: 会话 ID

    Returns:
        True 表示已删除，False 表示会话不存在

    Raises:
        无：所有异常被捕获并记录日志，返回 False
    """
    try:
        # 先查询会话是否存在（含已软删除的）
        session = db.execute(
            select(AISession).where(AISession.id == session_id)
        ).scalars().first()

        if session is None:
            return False

        # 缓存消息 ID 用于删除审计记录
        msg_ids = list(
            db.execute(
                select(AIMessage.id).where(AIMessage.session_id == session_id)
            ).scalars().all()
        )

        # 按外键依赖顺序删除：审计 → 消息 → 会话
        if msg_ids:
            db.execute(
                delete(AIActionAudit).where(AIActionAudit.message_id.in_(msg_ids))
            )
        db.execute(delete(AIMessage).where(AIMessage.session_id == session_id))
        db.execute(delete(AISession).where(AISession.id == session_id))
        db.commit()

        logger.info(
            "delete_session_permanently: session_id=%s deleted (with %s messages)",
            session_id, len(msg_ids),
        )
        return True
    except Exception as exc:
        logger.warning(
            "delete_session_permanently failed (session_id=%s): %s", session_id, exc
        )
        db.rollback()
        return False


__all__ = [
    "get_retention_days",
    "cleanup_expired_sessions",
    "delete_session_permanently",
]
