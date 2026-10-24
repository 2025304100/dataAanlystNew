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
import hashlib
from copy import deepcopy
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
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

_INSPECTION_PAGE = "portfolio-inspection"
_INSPECTION_ACCESS = "inspection_access"


def _is_inspection_page(source_page):
    return str(source_page or "").strip().lower() == _INSPECTION_PAGE


def _inspection_association(pack):
    """Only internal controlled packs can create this persisted access marker."""
    scope = pack.metadata.get("scope")
    actor = pack.metadata.get("access_actor")
    if (not isinstance(scope, dict) or type(scope.get("portfolio_id")) is not int or scope["portfolio_id"] <= 0
        or not isinstance(actor, str) or not actor.strip() or len(actor) > 128
        or pack.references.get("portfolio_id") != scope["portfolio_id"]):
        raise HTTPException(403, detail={"error_code": "INSPECTION_SESSION_SCOPE_UNKNOWN",
            "message": "检查分析缺少服务端组合与操作者关联，请从组合检查页重新分析"})
    return {"version": 1, "portfolio_id": scope["portfolio_id"], "actor": actor.strip(),
        "symbol_ids": scope.get("symbol_ids", []), "trade_date": scope.get("trade_date"), "step": scope.get("step")}


def _session_inspection_access(db, session, x_user):
    """Ordinary sessions retain their contract; inspection sessions fail closed."""
    try:
        summary = json.loads(session.context_summary or "null")
    except (ValueError, TypeError):
        summary = None
    marker = summary.get(_INSPECTION_ACCESS) if isinstance(summary, dict) else None
    if not _is_inspection_page(session.source_page) and marker is None:
        return None
    from app.api.routes import portfolio_governance as governance
    actor = governance._require_actor(x_user)
    if (not isinstance(marker, dict) or marker.get("version") != 1
        or type(marker.get("portfolio_id")) is not int or marker["portfolio_id"] <= 0
        or not isinstance(marker.get("actor"), str) or not marker["actor"].strip()):
        raise HTTPException(403, detail={"error_code": "INSPECTION_SESSION_SCOPE_UNKNOWN",
            "message": "旧检查会话缺少可靠范围关联，请从原组合重新发起分析"})
    if actor != marker["actor"]:
        raise HTTPException(403, detail={"error_code": "INSPECTION_SESSION_FORBIDDEN", "message": "不能访问其他操作者的检查分析会话"})
    governance._load_portfolio(db, marker["portfolio_id"])
    governance._ensure_portfolio_access(db, marker["portfolio_id"], actor)
    return marker


def _actual_evidence_refs(value):
    found = []
    def collect(item):
        if isinstance(item, dict):
            if isinstance(item.get("evidence_id"), str):
                found.append(item["evidence_id"])
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
    collect(value)
    return list(dict.fromkeys(found))[:200]


def _redact_inspection_keys(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if ai_audit._is_sensitive_key(str(key)) else _redact_inspection_keys(child)
            for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_inspection_keys(child) for child in value]
    return value


def _prepare_inspection_pack(pack, max_tokens):
    """Mutate the SAME object used by workflow audit to match the sent material."""
    from app.services.ai.context_pack import _estimate_tokens, _limit_size, sanitize_context
    association = _inspection_association(pack)
    scope, actor = deepcopy(pack.metadata["scope"]), association["actor"]
    sanitize_context(pack)
    pack.untrusted_content = _redact_inspection_keys(pack.untrusted_content)
    pack.metadata = _redact_inspection_keys(pack.metadata)
    original = deepcopy(pack.untrusted_content)
    unknowns = [str(item)[:512] for item in pack.metadata.get("unknowns", [])[:20]]
    if len(pack.metadata.get("unknowns", [])) > 20:
        unknowns.append("另有未知项未进入本次预算，请回正式检查页核实")
    _limit_size(pack, max_tokens)
    pack.metadata["scope"], pack.metadata["access_actor"] = scope, actor
    pack.capabilities = {"execute_tools": False, "human_confirmation_required": True}
    if pack.untrusted_content != original or pack.metadata.get("_size_truncated") or pack.metadata.get("_truncated_fields"):
        unknowns.append("模型上下文预算不足，部分材料未进入本次分析，须回正式证据页核实")
    # Finalize after every material reduction, including the hash and references.
    # The generic limiter may omit an entire evidence block; such a block must
    # never remain listed as evidence that the model consumed.
    while True:
        if not pack.untrusted_content:
            unknowns.append("本次未载入可核验事实材料，不能据此诊断或生成执行建议")
        pack.metadata["unknowns"] = list(dict.fromkeys(unknowns))
        pack.metadata["evidence_refs"] = _actual_evidence_refs(pack.untrusted_content)
        pack.metadata["inspection_snapshot_id"] = hashlib.sha256(json.dumps({"scope": scope,
            "material": pack.untrusted_content}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        if _estimate_tokens(asdict(pack)) <= max_tokens:
            return pack
        if pack.untrusted_content:
            pack.untrusted_content.pop()
            unknowns.append("模型上下文预算不足，部分材料未进入本次分析，须回正式证据页核实")
            continue
        # A large source-as-of/unknown summary can itself exhaust the budget.
        # Keep authorization identity and explicit missing-material status.
        pack.metadata = {key: pack.metadata.get(key) for key in ("version", "scope", "access_actor", "generated_at",
            "data_as_of", "inspection_snapshot_id", "evidence_refs")}
        pack.metadata["available_actions"] = []
        unknowns = ["上下文预算不足，本次没有可核验事实材料，请回组合检查页查看"]
        pack.data_status = {"credibility": "unknown", "missing_items": unknowns}
        pack.scoring_info = {}; pack.market_summary = None; pack.backtest_summary = None; pack.performance_summary = None
        pack.metadata["unknowns"] = unknowns
        if _estimate_tokens(asdict(pack)) > max_tokens:
            raise HTTPException(422, detail={"error_code": "INSPECTION_AI_BUDGET_TOO_SMALL",
                "message": "模型预算不足以保留分析范围和必要证据状态，请调整配置或回正式检查页核实"})


def _inspection_response(ai_resp, pack, trusted_metadata):
    """Model text is untrusted; authority, evidence scope and actions are ours."""
    ai_resp.metadata = deepcopy(trusted_metadata)
    allowed = {item["action_type"]: item for item in pack.metadata.get("available_actions", [])
        if isinstance(item, dict) and isinstance(item.get("action_type"), str)}
    selected = []
    for item in ai_resp.suggested_actions:
        action = item.get("action_type") if isinstance(item, dict) else None
        if action in allowed and action not in {value["action_type"] for value in selected}:
            # Never carry model-controlled URL/payload/portfolio IDs into a tool.
            selected.append({"action_type": action, "description": allowed[action].get("label", action)})
    ai_resp.suggested_actions = selected
    if ai_resp.draft:
        ai_resp.warnings.append("检查分析仅提供导航建议，模型草稿未进入业务执行或审批")
    ai_resp.draft = None
    if not pack.untrusted_content:
        ai_resp.answer = "我不知道：本次预算未能载入可核验材料，请回组合检查页查看证据或调整上下文预算。"
        ai_resp.evidence = []
        ai_resp.suggested_actions = []
    return ai_resp


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
    return create_session_with_context(payload, db)


def create_session_with_context(payload: AiSessionCreateRequest, db: Session, *, controlled_pack=None):
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
    association = None
    if controlled_pack is not None:
        association = _inspection_association(controlled_pack)
        from app.api.routes import portfolio_governance as governance
        governance._load_portfolio(db, association["portfolio_id"])
        governance._ensure_portfolio_access(db, association["portfolio_id"], association["actor"])
    elif _is_inspection_page(payload.source_page):
        raise HTTPException(403, detail={"error_code": "INSPECTION_SERVER_CONTEXT_REQUIRED",
            "message": "请从组合检查与对账入口发起分析，不能提交客户端检查快照"})

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
        profile = mgr.select_profile(db, purpose=payload.purpose)
        if profile is None:
            raise UnifiedErrorException(
                "AI_CONFIG_MISSING",
                status_code=400,
                override_user_message="AI 助手未配置，请前往设置",
            )

    if controlled_pack is not None:
        _prepare_inspection_pack(controlled_pack, profile.max_context_tokens)

    # ── 2. 标题与创建会话 ──
    title = payload.title or (first_message[:80] if first_message else "新 AI 会话")
    try:
        session = ai_session_service.create_session(
            db,
            title=title,
            source_page=_INSPECTION_PAGE if association else payload.source_page,
            provider=profile.provider,
            model=profile.model,
            profile_id=str(profile.id),
            context_summary=json.dumps({_INSPECTION_ACCESS: association}, ensure_ascii=False) if association else None,
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
        pack = controlled_pack if controlled_pack is not None else build_context_pack(
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
    model_called = controlled_pack is None or bool(pack.untrusted_content)
    if model_called:
        llm_messages = [
            {"role": "system", "content": _build_system_prompt(pack)},
            {"role": "user", "content": first_message},
        ]
        llm_result = call_llm_with_failover(
            db, llm_messages, profile_id=profile.id,
            purpose=payload.purpose, max_tokens=profile.max_tokens,
        )
    else:
        from app.services.ai.llm_client import LLMCallResult
        llm_result = LLMCallResult(success=True, raw_response=json.dumps({"answer": "我不知道：本次未载入可核验材料。"}))

    # ── 6. 解析响应 ──
    used_profile = llm_result.profile_used or profile
    context_metadata = {
        "data_as_of": pack.metadata.get("data_as_of") if pack else None,
        "model_version": pack.metadata.get("model_version") if pack else None,
        "rule_version": pack.metadata.get("rule_version") if pack else None,
        "provider_used": used_profile.provider if model_called else None,
        "model_used": used_profile.model if model_called else None,
        "latency_ms": llm_result.latency_ms,
        "tokens": llm_result.total_tokens,
    }
    if controlled_pack is not None:
        context_metadata = {**deepcopy(pack.metadata), **context_metadata, "model_called": model_called}

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
            if controlled_pack is not None:
                context_metadata.update(failover_from=llm_result.failover_from, failover_to=llm_result.failover_to,
                    failover_happened=True)
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

    if controlled_pack is not None:
        _inspection_response(ai_resp, pack, context_metadata)

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
        model_used=used_profile.model if model_called else None,
        provider_used=used_profile.provider if model_called else None,
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
def create_session_stream(payload: AiSessionCreateRequest, db: Session = Depends(get_db),
                          x_user: str | None = Query(default=None, alias="X-User")):
    """Create a session and push model output as server-sent events."""
    first_message = payload.first_message or payload.message
    if not first_message:
        raise HTTPException(status_code=422, detail="流式会话必须包含消息")
    context_refs = payload.context or payload.references or {}
    mgr = get_failover_manager()
    session = ai_session_service.get_session(db, payload.session_id) if payload.session_id else None
    if payload.session_id and session is None:
        raise HTTPException(status_code=404, detail="AI 会话不存在或已删除")
    association = _session_inspection_access(db, session, x_user) if session else None
    if session is None and _is_inspection_page(payload.source_page):
        raise HTTPException(403, detail={"error_code": "INSPECTION_SERVER_CONTEXT_REQUIRED",
            "message": "请从组合检查入口获取服务端材料后分析"})
    profile = (
        ai_profile_service.get_profile(db, int(session.profile_id))
        if session is not None and session.profile_id and str(session.profile_id).isdigit()
        else (ai_profile_service.get_profile(db, payload.profile_id) if payload.profile_id else mgr.select_profile(db, purpose=payload.purpose))
    )
    if profile is None or not profile.is_enabled:
        raise UnifiedErrorException("AI_CONFIG_MISSING", status_code=400, override_user_message="AI 助手未配置，请前往设置")

    if session is None:
        title = payload.title or first_message[:80]
        session = ai_session_service.create_session(
            db, title=title, source_page=payload.source_page,
            provider=profile.provider, model=profile.model, profile_id=str(profile.id),
        )
    if association:
        from app.services.portfolio_inspection_assistant import build_inspection_context
        # Continue only from current server evidence for the ORIGINAL scope.
        # Client source_page/context/references cannot retarget this session.
        pack = build_inspection_context(db, association["portfolio_id"], symbol_ids=association.get("symbol_ids"),
            trade_date=association.get("trade_date"), step=association.get("step"))
        pack.metadata["access_actor"] = association["actor"]
        _prepare_inspection_pack(pack, profile.max_context_tokens)
    else:
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
    history_messages = [
        {"role": msg.role, "content": msg.content}
        for msg in ai_session_service.list_messages(db, session.id, limit=100)
        if msg.role in (ROLE_USER, ROLE_ASSISTANT) and msg.content
    ]
    llm_messages = [{"role": "system", "content": _build_system_prompt(pack)}, *history_messages]
    # The current user message is persisted above; history_messages already
    # contains it, so the model receives the complete conversation exactly once.

    def event_stream():
        yield _sse("session", {"session_id": session.id, "title": session.title})
        if association and not pack.untrusted_content:
            metadata = {**deepcopy(pack.metadata), "provider_used": None, "model_used": None,
                "latency_ms": 0, "tokens": 0, "model_called": False}
            ai_resp = _inspection_response(build_response(answer="我不知道：未载入事实材料", metadata=metadata), pack, metadata)
            ai_session_service.add_message(db, session_id=session.id, role=ROLE_ASSISTANT,
                content=ai_resp.to_message_content(), context_summary=context_summary,
                metadata_json=json.dumps(metadata, ensure_ascii=False))
            yield _sse("done", {"session_id": session.id, "response": ai_resp.to_dict()})
            return
        result = None
        raw_stream = ""
        displayed_answer = ""
        for event in stream_llm_completion(
            db, llm_messages, profile_id=profile.id, max_tokens=profile.max_tokens,
            purpose=payload.purpose,
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
        used_profile = result.profile_used or profile
        metadata = {
            "data_as_of": pack.metadata.get("data_as_of") if pack else None,
            "model_version": pack.metadata.get("model_version") if pack else None,
            "rule_version": pack.metadata.get("rule_version") if pack else None,
            "provider_used": used_profile.provider, "model_used": used_profile.model,
            "latency_ms": result.latency_ms, "tokens": result.total_tokens,
        }
        if association:
            metadata = {**deepcopy(pack.metadata), **metadata, "model_called": True}
        ai_resp = parse_llm_response(result.raw_response, metadata)
        if result.failover_happened:
            ai_resp.warnings.append(f"已从 {result.failover_from} 切换到备用模型 {result.failover_to}")
            ai_resp.metadata.update(failover_from=result.failover_from, failover_to=result.failover_to, failover_happened=True)
            if association:
                metadata.update(failover_from=result.failover_from, failover_to=result.failover_to, failover_happened=True)
        if association:
            _inspection_response(ai_resp, pack, metadata)
        assistant_msg = ai_session_service.add_message(
            db, session_id=session.id, role=ROLE_ASSISTANT,
            content=ai_resp.to_message_content(), context_summary=context_summary,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens, latency_ms=result.latency_ms,
            model_used=used_profile.model, provider_used=used_profile.provider,
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
    x_user: str | None = Query(default=None, alias="X-User"),
):
    """列出会话（默认仅 active，按创建时间倒序）。"""
    sessions = ai_session_service.list_sessions(
        db, limit=limit, offset=offset, include_archived=include_archived
    )
    visible = []
    for session in sessions:
        try:
            _session_inspection_access(db, session, x_user)
        except HTTPException:
            continue
        visible.append(_session_to_dict(session))
    return {
        "items": visible,
        "limit": limit,
        "offset": offset,
        "include_archived": include_archived,
    }


@router.get("/ai/sessions/{session_id}")
def get_session_detail(session_id: int, db: Session = Depends(get_db),
                       x_user: str | None = Query(default=None, alias="X-User")):
    """获取会话详情。"""
    session = ai_session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")
    _session_inspection_access(db, session, x_user)
    return _session_to_dict(session)


@router.get("/ai/sessions/{session_id}/messages")
def list_session_messages(
    session_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    x_user: str | None = Query(default=None, alias="X-User"),
):
    """获取会话消息列表。"""
    session = ai_session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")
    _session_inspection_access(db, session, x_user)
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
def delete_session(session_id: int, db: Session = Depends(get_db),
                    x_user: str | None = Query(default=None, alias="X-User")):
    """删除会话（永久删除，不可恢复）。

    物理删除会话及其所有消息和审计记录。
    与软删除不同，此操作不可恢复。
    """
    session = ai_session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")
    _session_inspection_access(db, session, x_user)

    ok = session_retention.delete_session_permanently(db, session_id)
    if not ok:
        raise HTTPException(status_code=500, detail="会话删除失败")
    return {"status": "ok", "message": "会话已永久删除", "session_id": session_id}


@router.get("/ai/sessions/{session_id}/audit")
def get_session_audit(
    session_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    x_user: str | None = Query(default=None, alias="X-User"),
):
    """获取会话审计记录（脱敏）。

    返回的审计记录已脱敏，不包含 API Key/Secret/Webhook 等敏感信息，
    也不包含完整 K 线/行情数据。
    """
    session = ai_session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")
    _session_inspection_access(db, session, x_user)

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
