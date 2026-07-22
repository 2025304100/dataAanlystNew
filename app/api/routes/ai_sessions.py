"""AI 会话与审计 API（WP-AI.6 结构化输出与审计）。

端点：
- GET    /api/v1/ai/sessions                  - 列出会话
- GET    /api/v1/ai/sessions/{id}             - 获取会话详情
- GET    /api/v1/ai/sessions/{id}/messages    - 获取消息列表
- DELETE /api/v1/ai/sessions/{id}             - 删除会话（永久删除）
- GET    /api/v1/ai/sessions/{id}/audit       - 获取审计记录（脱敏）
- POST   /api/v1/ai/sessions/cleanup          - 清理过期会话

约束：
- 永不返回明文 Secret：审计记录导出前已脱敏
- AI 失败不阻塞业务：清理失败返回 0，不抛 500
- 用户可主动删除会话（永久删除，不可恢复）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.ai_session import AIMessage, AISession
from app.services import ai_session_service
from app.services.ai import audit as ai_audit
from app.services.ai import session_retention

logger = logging.getLogger(__name__)

router = APIRouter()


# ── 响应序列化 ───────────────────────────────────────────────


def _session_to_dict(session: AISession) -> dict:
    """将 AISession 序列化为响应字典。"""
    return {
        "id": session.id,
        "title": session.title,
        "source_page": session.source_page,
        "provider": session.provider,
        "model": session.model,
        "profile_id": session.profile_id,
        "status": session.status,
        "context_summary": session.context_summary,
        "total_tokens": session.total_tokens,
        "total_cost": session.total_cost,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        "deleted_at": session.deleted_at.isoformat() if session.deleted_at else None,
    }


def _message_to_dict(msg: AIMessage) -> dict:
    """将 AIMessage 序列化为响应字典。"""
    return {
        "id": msg.id,
        "session_id": msg.session_id,
        "role": msg.role,
        "content": msg.content,
        "context_summary": msg.context_summary,
        "prompt_tokens": msg.prompt_tokens,
        "completion_tokens": msg.completion_tokens,
        "total_tokens": msg.total_tokens,
        "latency_ms": msg.latency_ms,
        "model_used": msg.model_used,
        "provider_used": msg.provider_used,
        "metadata_json": msg.metadata_json,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


# ── 端点 ───────────────────────────────────────────────────


@router.get("/ai/sessions")
def list_sessions(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
):
    """列出会话（默认仅 active，按创建时间倒序）。"""
    sessions = ai_session_service.list_sessions(
        db, limit=limit, offset=offset, include_archived=include_archived
    )
    return {
        "items": [_session_to_dict(s) for s in sessions],
        "limit": limit,
        "offset": offset,
        "include_archived": include_archived,
    }


@router.get("/ai/sessions/{session_id}")
def get_session_detail(session_id: int, db: Session = Depends(get_db)):
    """获取会话详情。"""
    session = ai_session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")
    return _session_to_dict(session)


@router.get("/ai/sessions/{session_id}/messages")
def list_session_messages(
    session_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """获取会话消息列表。"""
    session = ai_session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")
    messages = ai_session_service.list_messages(
        db, session_id, limit=limit, offset=offset
    )
    return {
        "items": [_message_to_dict(m) for m in messages],
        "session_id": session_id,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/ai/sessions/{session_id}")
def delete_session(session_id: int, db: Session = Depends(get_db)):
    """删除会话（永久删除，不可恢复）。

    物理删除会话及其所有消息和审计记录。
    与软删除不同，此操作不可恢复。
    """
    session = ai_session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")

    ok = session_retention.delete_session_permanently(db, session_id)
    if not ok:
        raise HTTPException(status_code=500, detail="会话删除失败")
    return {"status": "ok", "message": "会话已永久删除", "session_id": session_id}


@router.get("/ai/sessions/{session_id}/audit")
def get_session_audit(
    session_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """获取会话审计记录（脱敏）。

    返回的审计记录已脱敏，不包含 API Key/Secret/Webhook 等敏感信息，
    也不包含完整 K 线/行情数据。
    """
    session = ai_session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")

    # 导出脱敏后的审计记录
    records = ai_audit.export_audit_records(db, session_id)
    # 应用 limit（export_audit_records 内部已限制 500，此处再裁剪）
    records = records[:limit]
    return {
        "items": records,
        "session_id": session_id,
        "count": len(records),
    }


@router.post("/ai/sessions/cleanup")
def cleanup_expired_sessions(db: Session = Depends(get_db)):
    """清理过期会话（将超过保留期的 active 会话标记为 archived）。

    AI 失败不阻塞业务：清理失败时返回 cleaned=0，不抛 500。
    """
    try:
        cleaned = session_retention.cleanup_expired_sessions(db)
        retention_days = session_retention.get_retention_days()
        return {
            "status": "ok",
            "cleaned": cleaned,
            "retention_days": retention_days,
            "message": f"已归档 {cleaned} 个过期会话（保留期 {retention_days} 天）",
        }
    except Exception as exc:
        logger.warning("cleanup_expired_sessions API failed: %s", exc)
        return {
            "status": "ok",
            "cleaned": 0,
            "retention_days": session_retention.get_retention_days(),
            "message": f"清理失败：{exc}",
        }


__all__ = ["router"]
