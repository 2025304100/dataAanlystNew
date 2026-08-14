"""AI 会话与审计 API（WP-AI.6 结构化输出与审计 / WP-AI.7 会话创建）。

端点：
- POST   /api/v1/ai/sessions                  - 创建会话（可选同时发送首条消息）
- GET    /api/v1/ai/sessions                  - 列出会话
- GET    /api/v1/ai/sessions/{id}             - 获取会话详情
- GET    /api/v1/ai/sessions/{id}/messages    - 获取消息列表
- DELETE /api/v1/ai/sessions/{id}             - 删除会话（永久删除）
- GET    /api/v1/ai/sessions/{id}/audit       - 获取审计记录（脱敏）
- POST   /api/v1/ai/sessions/cleanup          - 清理过期会话

约束：
- 永不返回明文 Secret：审计记录导出前已脱敏，上下文包构造时已脱敏
- AI 失败不阻塞业务：清理失败返回 0，不抛 500；模型全失败返回 503 + 降级响应
- 三步确认：副作用操作只生成草稿（draft 字段），不直接写业务状态
- 用户可主动删除会话（永久删除，不可恢复）
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.ai_profile import PURPOSE_ALL
from app.models.ai_session import (
    ACTION_TYPES,
    AIMessage,
    AISession,
    ROLE_ASSISTANT,
    ROLE_USER,
)
from app.schemas.ai_session import AiSessionCreateRequest
from app.schemas.error_sanitizer import sanitize_message
from app.schemas.errors import TechnicalDetails, UnifiedErrorException
from app.services import ai_profile_service, ai_session_service
from app.services.ai import audit as ai_audit
from app.services.ai import session_retention
from app.services.ai.context_pack import build_context_pack, to_prompt_dict
from app.services.ai.llm_client import call_llm_with_failover, stream_llm_completion
from app.services.ai.response import build_response, parse_llm_response
from app.services.ai_failover import get_failover_manager

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


# ── 创建会话辅助函数 ───────────────────────────────────────


_SYSTEM_PROMPT_BASE = (
    "你是一个 A 股量化投资助手。请基于提供的受控上下文回答用户问题。"
    "回答必须是统一 JSON 结构："
    '{"answer": "...", "evidence": [...], "warnings": [...], '
    '"suggested_actions": [...], "draft": null}。'
    "对于涉及副作用操作（添加指标/筛选器/告警/备注/复盘/下单），"
    "只能在 draft 字段生成建议草稿，不得直接执行任何写操作。"
    "缺数据时 answer 必须明确说\"我不知道\"，并在 warnings 中提示数据不足。"
)


_SYSTEM_PROMPT_BASE += (
    "evidence 每一项必须严格包含 type、source、content、confidence 四个字段；"
    "content 必须是可直接展示的证据文字，confidence 为 0 到 1 的数字或 null。"
)


def _build_system_prompt(pack) -> str:
    """根据受控上下文包构造 system prompt（已脱敏）。"""
    if pack is None:
        return _SYSTEM_PROMPT_BASE
    try:
        ctx = to_prompt_dict(pack)
        return (
            _SYSTEM_PROMPT_BASE
            + "\n\n受控上下文（JSON，已脱敏）：\n"
            + json.dumps(ctx, ensure_ascii=False, default=str)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build system prompt failed: %s", exc)
        return _SYSTEM_PROMPT_BASE


def _context_summary_for_audit(pack) -> str | None:
    """将上下文包压缩为审计摘要 JSON 字符串（脱敏，不含敏感/大字段）。"""
    if pack is None:
        return None
    try:
        summary = ai_audit.summarize_context_for_audit(pack)
        return json.dumps(summary, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("summarize context for audit failed: %s", exc)
        return None


def _maybe_audit_draft(db, session_id, message_id, pack, ai_resp) -> None:
    """若 AI 响应含草稿（副作用操作），写入审计记录（只生成草稿，不执行）。

    三步确认流程第一步：AI 建议 → 审计记录（user_confirmed=False）。
    执行需用户后续确认，本端点不执行任何副作用。
    """
    if not ai_resp.draft:
        return
    draft = ai_resp.draft if isinstance(ai_resp.draft, dict) else {"value": ai_resp.draft}
    action_type = str(
        draft.get("draft_type") or draft.get("action_type") or "draft_note"
    )
    if action_type not in ACTION_TYPES:
        action_type = "draft_note"
    try:
        ai_audit.create_audit_record(
            db,
            session_id=session_id,
            message_id=message_id,
            context_pack=pack if pack is not None else {"source_page": "task"},
            action_type=action_type,
            suggested_payload=draft,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create audit for draft failed: %s", exc)


def _build_create_response(session, messages_out, response_dict) -> dict:
    """构造创建会话响应体。"""
    return {
        "session_id": session.id,
        "title": session.title,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "messages": messages_out,
        "response": response_dict,
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _stream_answer_preview(raw: str) -> str:
    """Extract the answer value from a partial structured JSON response."""
    if not raw.lstrip().startswith("{"):
        return raw
    marker = raw.find('"answer"')
    if marker < 0:
        return ""
    colon = raw.find(":", marker + 8)
    start = raw.find('"', colon + 1)
    if colon < 0 or start < 0:
        return ""
    output: list[str] = []
    escaped = False
    escape_map = {"n": chr(10), "r": chr(13), "t": chr(9)}
    for char in raw[start + 1:]:
        if escaped:
            output.append(escape_map.get(char, char))
            escaped = False
        elif char == chr(92):
            escaped = True
        elif char == chr(34):
            break
        else:
            output.append(char)
    return "".join(output)


# ── 端点 ───────────────────────────────────────────────────


@router.post("/ai/sessions")
def create_session(
    payload: AiSessionCreateRequest,
    db: Session = Depends(get_db),
):
    """创建 AI 会话（可选同时发送首条消息）。

    流程：
    1. 创建 AISession（profile_id 未提供则查默认 Profile，无则 400 + 中文提示）
    2. 若有 first_message：构造受控上下文包（脱敏）→ 调用模型（主备降级）
       → 落库 user + assistant 消息 → 返回结构化响应
    3. 凭据脱敏：响应与审计只保存上下文摘要，不存完整 K 线/敏感配置
    4. 错误处理：Profile 未配置 400；模型全失败 503 + 降级响应；格式异常 500
    5. 三步确认：副作用操作只生成草稿（draft），不直接写业务状态

    兼容前端字段：message 等价于 first_message，references 等价于 context。
    """
    # 兼容前端字段归一化
    first_message = payload.first_message or payload.message
    context_refs = payload.context or payload.references or {}

    # ── 1. 解析 Profile ──
    mgr = get_failover_manager()
    if payload.profile_id is not None:
        profile = ai_profile_service.get_profile(db, payload.profile_id)
        if profile is None or not profile.is_enabled:
            raise UnifiedErrorException(
                "AI_CONFIG_MISSING",
                status_code=400,
                override_user_message="AI 助手未配置，请前往设置",
            )
    else:
        profile = mgr.select_profile(db, purpose=PURPOSE_ALL)
        if profile is None:
            raise UnifiedErrorException(
                "AI_CONFIG_MISSING",
                status_code=400,
                override_user_message="AI 助手未配置，请前往设置",
            )

    # ── 2. 标题与创建会话 ──
    title = payload.title or (first_message[:80] if first_message else "新 AI 会话")
    try:
        session = ai_session_service.create_session(
            db,
            title=title,
            source_page=payload.source_page,
            provider=profile.provider,
            model=profile.model,
            profile_id=str(profile.id),
        )
    except Exception as exc:
        logger.exception("create AI session failed")
        raise UnifiedErrorException(
            "UNKNOWN_ERROR",
            status_code=500,
            technical_details=TechnicalDetails(
                exception_type=type(exc).__name__,
                error_message=sanitize_message(str(exc))[:500],
            ),
        ) from exc

    # 无首条消息：直接返回空会话
    if not first_message:
        return _build_create_response(session, [], None)

    # ── 3. 构造受控上下文包（脱敏）──
    try:
        pack = build_context_pack(
            db,
            user_question=first_message,
            source_page=payload.source_page or "task",
            references=context_refs,
            max_context_tokens=profile.max_context_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_context_pack failed: %s", exc)
        pack = None

    context_summary = _context_summary_for_audit(pack)

    # ── 4. 落库 user 消息 ──
    user_msg = ai_session_service.add_message(
        db,
        session_id=session.id,
        role=ROLE_USER,
        content=first_message,
        context_summary=context_summary,
    )

    # ── 5. 调用模型（主备降级）──
    system_prompt = _build_system_prompt(pack)
    llm_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": first_message},
    ]

    llm_result = call_llm_with_failover(
        db,
        llm_messages,
        profile_id=profile.id,
        purpose=PURPOSE_ALL,
        max_tokens=profile.max_tokens,
    )

    # ── 6. 解析响应 ──
    used_profile = llm_result.profile_used or profile
    context_metadata = {
        "data_as_of": pack.metadata.get("data_as_of") if pack else None,
        "model_version": pack.metadata.get("model_version") if pack else None,
        "rule_version": pack.metadata.get("rule_version") if pack else None,
        "provider_used": used_profile.provider,
        "model_used": used_profile.model,
        "latency_ms": llm_result.latency_ms,
        "tokens": llm_result.total_tokens,
    }

    if llm_result.success:
        ai_resp = parse_llm_response(llm_result.raw_response, context_metadata)
        # 标注主备切换
        if llm_result.failover_happened:
            ai_resp.warnings.append(
                f"主模型 {llm_result.failover_from} 超时/限流，已切换到备用模型 {llm_result.failover_to}"
            )
            ai_resp.metadata["failover_from"] = llm_result.failover_from
            ai_resp.metadata["failover_to"] = llm_result.failover_to
            ai_resp.metadata["failover_happened"] = True
        http_status = 200
    else:
        # 模型全失败 → 503 + 降级响应（主备切换在 reply 中标注）
        if llm_result.failover_happened:
            degraded_answer = (
                f"AI 服务暂时不可用：主模型 {llm_result.failover_from} 与备用模型 "
                f"{llm_result.failover_to} 均不可用，请稍后重试。"
            )
        else:
            degraded_answer = "AI 服务暂时不可用，请稍后重试。"
        ai_resp = build_response(
            answer=degraded_answer,
            warnings=["AI 模型不可用，已降级响应"],
            suggested_actions=[
                {"action_type": "retry", "description": "稍后重试"}
            ],
            metadata=context_metadata,
        )
        http_status = 503

    # ── 7. 落库 assistant 消息 ──
    assistant_content = ai_resp.to_message_content()
    assistant_msg = ai_session_service.add_message(
        db,
        session_id=session.id,
        role=ROLE_ASSISTANT,
        content=assistant_content,
        context_summary=context_summary,
        prompt_tokens=llm_result.prompt_tokens,
        completion_tokens=llm_result.completion_tokens,
        total_tokens=llm_result.total_tokens,
        latency_ms=llm_result.latency_ms,
        model_used=used_profile.model,
        provider_used=used_profile.provider,
        metadata_json=json.dumps(ai_resp.metadata, ensure_ascii=False),
    )

    # ── 8. 审计：副作用操作只生成草稿 ──
    _maybe_audit_draft(db, session.id, assistant_msg.id, pack, ai_resp)

    messages_out = [_message_to_dict(user_msg), _message_to_dict(assistant_msg)]
    response_dict = ai_resp.to_dict()
    body = _build_create_response(session, messages_out, response_dict)

    # ── 9. 503 返回降级响应体（保留会话与消息）──
    if http_status == 503:
        return JSONResponse(status_code=503, content=body)
    return body


@router.post("/ai/sessions/stream")
def create_session_stream(payload: AiSessionCreateRequest, db: Session = Depends(get_db)):
    """Create a session and push model output as server-sent events."""
    first_message = payload.first_message or payload.message
    if not first_message:
        raise HTTPException(status_code=422, detail="流式会话必须包含消息")
    context_refs = payload.context or payload.references or {}
    mgr = get_failover_manager()
    profile = ai_profile_service.get_profile(db, payload.profile_id) if payload.profile_id else mgr.select_profile(db, purpose=PURPOSE_ALL)
    if profile is None or not profile.is_enabled:
        raise UnifiedErrorException("AI_CONFIG_MISSING", status_code=400, override_user_message="AI 助手未配置，请前往设置")

    title = payload.title or first_message[:80]
    session = ai_session_service.create_session(
        db, title=title, source_page=payload.source_page,
        provider=profile.provider, model=profile.model, profile_id=str(profile.id),
    )
    try:
        pack = build_context_pack(
            db, user_question=first_message, source_page=payload.source_page or "task",
            references=context_refs, max_context_tokens=profile.max_context_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_context_pack for stream failed: %s", exc)
        pack = None
    context_summary = _context_summary_for_audit(pack)
    ai_session_service.add_message(
        db, session_id=session.id, role=ROLE_USER,
        content=first_message, context_summary=context_summary,
    )
    llm_messages = [
        {"role": "system", "content": _build_system_prompt(pack)},
        {"role": "user", "content": first_message},
    ]

    def event_stream():
        yield _sse("session", {"session_id": session.id, "title": session.title})
        result = None
        raw_stream = ""
        displayed_answer = ""
        for event in stream_llm_completion(
            db, llm_messages, profile_id=profile.id, max_tokens=profile.max_tokens,
        ):
            if event["type"] == "delta":
                raw_stream += event["content"]
                answer = _stream_answer_preview(raw_stream)
                if len(answer) > len(displayed_answer):
                    yield _sse("delta", {"content": answer[len(displayed_answer):]})
                    displayed_answer = answer
            else:
                result = event["result"]
        if result is None or not result.success:
            message = result.error_message if result else "AI 流式响应异常结束"
            yield _sse("error", {"message": message})
            return
        metadata = {
            "data_as_of": pack.metadata.get("data_as_of") if pack else None,
            "model_version": pack.metadata.get("model_version") if pack else None,
            "rule_version": pack.metadata.get("rule_version") if pack else None,
            "provider_used": profile.provider, "model_used": profile.model,
            "latency_ms": result.latency_ms, "tokens": result.total_tokens,
        }
        ai_resp = parse_llm_response(result.raw_response, metadata)
        assistant_msg = ai_session_service.add_message(
            db, session_id=session.id, role=ROLE_ASSISTANT,
            content=ai_resp.to_message_content(), context_summary=context_summary,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens, latency_ms=result.latency_ms,
            model_used=profile.model, provider_used=profile.provider,
            metadata_json=json.dumps(ai_resp.metadata, ensure_ascii=False),
        )
        _maybe_audit_draft(db, session.id, assistant_msg.id, pack, ai_resp)
        yield _sse("done", {"session_id": session.id, "response": ai_resp.to_dict()})

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
