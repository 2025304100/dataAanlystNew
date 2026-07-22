"""AI 会话 CRUD 服务（WP-AI.1）。

提供 AISession / AIMessage / AIActionAudit 的基础增删改查能力。
不涉及 AI 调用本身（由上层 AI 客户端封装），仅负责数据持久化。

API 一览：
- create_session(db, title, source_page, provider, model) -> AISession
- add_message(db, session_id, role, content, ...) -> AIMessage
- get_session(db, session_id) -> AISession | None
- list_sessions(db, limit=20, offset=0, include_archived=False) -> list[AISession]
- archive_session(db, session_id) -> AISession
- delete_session(db, session_id) -> AISession  （软删除）
- add_action_audit(db, message_id, action_type, suggested_payload, ...) -> AIActionAudit
- confirm_action(db, audit_id, final_result) -> AIActionAudit
- reject_action(db, audit_id, reason) -> AIActionAudit

设计要点：
- 软删除：delete_session 仅设置 status='deleted' + deleted_at，不物理删除
- Token 累计：add_message 时同步累加 session.total_tokens
- 状态约束：archived 会话仍可查询（include_archived=True），
  deleted 会话不进入常规 list_sessions 结果
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.ai_session import (
    AISession,
    AIMessage,
    AIActionAudit,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    SESSION_STATUS_DELETED,
)


def _now_utc() -> datetime:
    """当前 UTC 时间（naive，与项目其他模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SessionNotFoundError(LookupError):
    """会话不存在或已被软删除。"""


class MessageNotFoundError(LookupError):
    """消息不存在。"""


class ActionAuditNotFoundError(LookupError):
    """动作审计记录不存在。"""


# ----------------------------------------------------------------------------
# 会话 CRUD
# ----------------------------------------------------------------------------


def create_session(
    db: Session,
    *,
    title: str,
    source_page: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    profile_id: str | None = None,
    context_summary: str | None = None,
) -> AISession:
    """创建 AI 会话。

    Args:
        db: 数据库会话
        title: 会话标题（必填）
        source_page: 来源页面（discovery/research/portfolio/backtest/task）
        provider: AI 提供商（openai/anthropic/ollama）
        model: 模型名称
        profile_id: AI Profile ID
        context_summary: 初始上下文摘要

    Returns:
        创建后的 AISession（含 id）
    """
    session = AISession(
        title=title,
        source_page=source_page,
        provider=provider,
        model=model,
        profile_id=profile_id,
        context_summary=context_summary,
        status=SESSION_STATUS_ACTIVE,
        total_tokens=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_session(db: Session, session_id: int) -> AISession | None:
    """按 ID 获取会话（含已归档，不含已软删除）。

    如需查询软删除的会话，直接使用 SQL 查询 ai_sessions 表。
    """
    stmt = select(AISession).where(
        and_(
            AISession.id == session_id,
            AISession.status != SESSION_STATUS_DELETED,
        )
    )
    return db.execute(stmt).scalars().first()


def list_sessions(
    db: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    include_archived: bool = False,
) -> list[AISession]:
    """列出会话（默认仅 active，按创建时间倒序）。

    Args:
        db: 数据库会话
        limit: 返回最大条数
        offset: 偏移量（分页）
        include_archived: 是否包含 archived 会话（deleted 始终不返回）

    Returns:
        AISession 列表
    """
    if include_archived:
        statuses = [SESSION_STATUS_ACTIVE, SESSION_STATUS_ARCHIVED]
    else:
        statuses = [SESSION_STATUS_ACTIVE]

    stmt = (
        select(AISession)
        .where(AISession.status.in_(statuses))
        .order_by(AISession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.execute(stmt).scalars().all())


def archive_session(db: Session, session_id: int) -> AISession:
    """归档会话（active → archived）。

    归档后会话不再出现在默认列表，但仍可查询与继续对话。
    已归档或已删除的会话重复归档不报错（幂等）。

    Raises:
        SessionNotFoundError: 会话不存在或已软删除
    """
    session = get_session(db, session_id)
    if session is None:
        raise SessionNotFoundError(f"会话不存在或已删除: session_id={session_id}")

    if session.status != SESSION_STATUS_ARCHIVED:
        session.status = SESSION_STATUS_ARCHIVED
        db.commit()
        db.refresh(session)
    return session


def delete_session(db: Session, session_id: int) -> AISession:
    """软删除会话（设置 status='deleted' + deleted_at）。

    物理删除由后续清理任务负责，本服务不提供物理删除接口。

    Raises:
        SessionNotFoundError: 会话不存在或已软删除
    """
    session = get_session(db, session_id)
    if session is None:
        raise SessionNotFoundError(f"会话不存在或已删除: session_id={session_id}")

    if session.status != SESSION_STATUS_DELETED:
        session.status = SESSION_STATUS_DELETED
        session.deleted_at = _now_utc()
        db.commit()
        db.refresh(session)
    return session


# ----------------------------------------------------------------------------
# 消息 CRUD
# ----------------------------------------------------------------------------


def add_message(
    db: Session,
    *,
    session_id: int,
    role: str,
    content: str,
    context_summary: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    latency_ms: int | None = None,
    model_used: str | None = None,
    provider_used: str | None = None,
    metadata_json: str | None = None,
) -> AIMessage:
    """向会话追加一条消息，并同步累计会话级 total_tokens。

    Args:
        db: 数据库会话
        session_id: 目标会话 ID
        role: 角色（user/assistant/system）
        content: 消息内容
        context_summary: 上下文摘要
        prompt_tokens: 提示 Token 数
        completion_tokens: 补全 Token 数
        total_tokens: 总 Token 数；为 None 时自动取 prompt + completion
        latency_ms: 响应耗时毫秒
        model_used: 实际使用的模型
        provider_used: 实际使用的提供商
        metadata_json: 元数据 JSON 字符串

    Raises:
        SessionNotFoundError: 会话不存在或已软删除
    """
    session = get_session(db, session_id)
    if session is None:
        raise SessionNotFoundError(f"会话不存在或已删除: session_id={session_id}")

    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens

    message = AIMessage(
        session_id=session_id,
        role=role,
        content=content,
        context_summary=context_summary,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        model_used=model_used,
        provider_used=provider_used,
        metadata_json=metadata_json,
    )
    db.add(message)

    # 同步累加会话级 token 计数
    session.total_tokens = (session.total_tokens or 0) + total_tokens

    db.commit()
    db.refresh(message)
    return message


def list_messages(
    db: Session,
    session_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[AIMessage]:
    """列出会话消息（按 id 升序，即创建顺序）。"""
    stmt = (
        select(AIMessage)
        .where(AIMessage.session_id == session_id)
        .order_by(AIMessage.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.execute(stmt).scalars().all())


# ----------------------------------------------------------------------------
# 动作审计 CRUD
# ----------------------------------------------------------------------------


def add_action_audit(
    db: Session,
    *,
    message_id: int,
    action_type: str,
    suggested_payload: str | dict[str, Any],
    preview_result: str | dict[str, Any] | None = None,
) -> AIActionAudit:
    """为 assistant 消息追加一条动作审计记录。

    Args:
        db: 数据库会话
        message_id: 关联的 AIMessage.id
        action_type: 动作类型（draft_indicator/draft_filter/draft_alert/
                     draft_note/draft_review/draft_order）
        suggested_payload: AI 建议的 payload（dict 自动序列化为 JSON 字符串）
        preview_result: 预览结果（dict 自动序列化为 JSON 字符串）

    Returns:
        创建后的 AIActionAudit
    """
    import json

    if isinstance(suggested_payload, dict):
        suggested_payload = json.dumps(suggested_payload, ensure_ascii=False)
    if isinstance(preview_result, dict):
        preview_result = json.dumps(preview_result, ensure_ascii=False)

    audit = AIActionAudit(
        message_id=message_id,
        action_type=action_type,
        suggested_payload=suggested_payload,
        preview_result=preview_result,
        user_confirmed=False,
    )
    db.add(audit)
    db.commit()
    db.refresh(audit)
    return audit


def get_action_audit(db: Session, audit_id: int) -> AIActionAudit | None:
    """按 ID 获取动作审计记录。"""
    return db.execute(
        select(AIActionAudit).where(AIActionAudit.id == audit_id)
    ).scalars().first()


def confirm_action(
    db: Session,
    audit_id: int,
    final_result: str | dict[str, Any] | None = None,
) -> AIActionAudit:
    """确认动作（user_confirmed=True，记录最终结果）。

    Raises:
        ActionAuditNotFoundError: 审计记录不存在
    """
    import json

    audit = get_action_audit(db, audit_id)
    if audit is None:
        raise ActionAuditNotFoundError(f"动作审计记录不存在: audit_id={audit_id}")

    if isinstance(final_result, dict):
        final_result = json.dumps(final_result, ensure_ascii=False)

    audit.user_confirmed = True
    audit.confirmed_at = _now_utc()
    audit.final_result = final_result
    audit.rejected_reason = None  # 确认时清空拒绝原因（允许重新确认覆盖拒绝）
    db.commit()
    db.refresh(audit)
    return audit


def reject_action(
    db: Session,
    audit_id: int,
    reason: str | None = None,
) -> AIActionAudit:
    """拒绝动作（user_confirmed=False，记录拒绝原因）。

    Raises:
        ActionAuditNotFoundError: 审计记录不存在
    """
    audit = get_action_audit(db, audit_id)
    if audit is None:
        raise ActionAuditNotFoundError(f"动作审计记录不存在: audit_id={audit_id}")

    audit.user_confirmed = False
    audit.confirmed_at = _now_utc()
    audit.rejected_reason = reason
    audit.final_result = None  # 拒绝时清空最终结果（允许重新拒绝覆盖确认）
    db.commit()
    db.refresh(audit)
    return audit


__all__ = [
    # 异常
    "SessionNotFoundError",
    "MessageNotFoundError",
    "ActionAuditNotFoundError",
    # 会话 CRUD
    "create_session",
    "get_session",
    "list_sessions",
    "archive_session",
    "delete_session",
    # 消息 CRUD
    "add_message",
    "list_messages",
    # 动作审计 CRUD
    "add_action_audit",
    "get_action_audit",
    "confirm_action",
    "reject_action",
]
