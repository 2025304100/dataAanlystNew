"""AI 草案确认流程 API（WP4-05）。

通用三步确认端点，适用于所有 DRAFT_REGISTRY 注册的草稿类型
（draft_indicator/draft_filter/draft_alert/draft_note/draft_review/
draft_order/draft_factor）。

端点：
- GET    /api/v1/ai/drafts/{audit_id}                - 获取草案详情（含原始建议与当前 payload 用于差异对比）
- POST   /api/v1/ai/drafts/{audit_id}/preview        - 重新校验预览（dry-run，可携带用户修改后的 payload）
- POST   /api/v1/ai/drafts/{audit_id}/confirm        - 确认草案（user_confirmed=True，可携带修改后的 payload）
- POST   /api/v1/ai/drafts/{audit_id}/reject         - 拒绝草案（记录原因）
- POST   /api/v1/ai/drafts/{audit_id}/execute        - 执行已确认草案（调用对应 execute_xxx）

关键约束（对齐 spec.md WP4 Requirements）：
- AI 草案不自动入库：execute 前必须 confirm（user_confirmed=True）
- 用户可修改 payload：confirm 时携带 modified_payload，原始建议保存在 preview_result
- 差异可追溯：GET 端点返回 original_suggested_payload 与 current_payload
- 幂等：重复 execute 返回既有结果（由各 execute_xxx 保证）
- AI 无生产权限：execute 只创建 draft 状态，不自动 transition/activate
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.ai_session import AIActionAudit
from app.services.ai.drafts import DRAFT_REGISTRY, get_draft_functions
from app.services.ai_session_service import (
    ActionAuditNotFoundError,
    confirm_action,
    get_action_audit,
    reject_action,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── 请求/响应模型 ───────────────────────────────────────────


class PreviewRequest(BaseModel):
    """预览请求（可携带用户修改后的 payload）。"""

    modified_payload: dict[str, Any] | None = None


class ConfirmRequest(BaseModel):
    """确认请求（可携带用户修改后的 payload）。"""

    modified_payload: dict[str, Any] | None = None
    reason: str | None = None


class RejectRequest(BaseModel):
    """拒绝请求。"""

    reason: str | None = None


# ── 辅助函数 ───────────────────────────────────────────────


def _load_audit(db: Session, audit_id: int) -> AIActionAudit:
    """加载审计记录，不存在时抛 404。"""
    audit = get_action_audit(db, audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="audit_not_found")
    return audit


def _parse_payload(audit: AIActionAudit) -> dict[str, Any]:
    """解析审计记录的 suggested_payload JSON。"""
    try:
        payload = json.loads(audit.suggested_payload)
        if not isinstance(payload, dict):
            return {}
        return payload
    except (ValueError, TypeError):
        return {}


def _parse_original_payload(audit: AIActionAudit) -> dict[str, Any] | None:
    """从 preview_result 中提取原始 AI 建议 payload（确认时修改过则存在）。"""
    if not audit.preview_result:
        return None
    try:
        data = json.loads(audit.preview_result)
        if isinstance(data, dict):
            original = data.get("original_suggested_payload")
            if isinstance(original, dict):
                return original
    except (ValueError, TypeError):
        pass
    return None


def _audit_to_dict(audit: AIActionAudit) -> dict[str, Any]:
    """将审计记录序列化为响应字典（含原始建议与当前 payload）。"""
    current_payload = _parse_payload(audit)
    original_payload = _parse_original_payload(audit)
    # 若未修改过，原始就是当前
    if original_payload is None:
        original_payload = current_payload

    final_result: Any = None
    if audit.final_result:
        try:
            final_result = json.loads(audit.final_result)
        except (ValueError, TypeError):
            final_result = audit.final_result

    return {
        "audit_id": audit.id,
        "message_id": audit.message_id,
        "action_type": audit.action_type,
        "original_suggested_payload": original_payload,
        "current_payload": current_payload,
        "was_modified": original_payload != current_payload,
        "user_confirmed": audit.user_confirmed,
        "confirmed_at": audit.confirmed_at.isoformat() if audit.confirmed_at else None,
        "final_result": final_result,
        "rejected_reason": audit.rejected_reason,
        "created_at": audit.created_at.isoformat() if audit.created_at else None,
    }


# ── 端点 ───────────────────────────────────────────────────


@router.get("/ai/drafts/{audit_id}")
def get_draft_detail(audit_id: int, db: Session = Depends(get_db)):
    """获取草案详情（含原始建议与当前 payload，用于前端差异对比）。

    返回：
    - original_suggested_payload：AI 原始建议
    - current_payload：当前 payload（用户修改后则与原始不同）
    - was_modified：是否被用户修改过
    - user_confirmed：是否已确认
    - final_result：执行结果（已执行时存在）
    """
    audit = _load_audit(db, audit_id)
    return _audit_to_dict(audit)


@router.post("/ai/drafts/{audit_id}/preview")
def preview_draft(
    audit_id: int,
    payload: PreviewRequest | None = None,
    db: Session = Depends(get_db),
):
    """重新校验预览（dry-run，不写 DB）。

    可携带 modified_payload 对用户修改后的字段做校验，
    不传则用审计记录中的当前 payload。

    返回对应 draft_type 的 preview_xxx 结果：
    {is_valid, errors, changes, extra?}
    """
    audit = _load_audit(db, audit_id)
    draft_type = audit.action_type
    funcs = get_draft_functions(draft_type)
    if funcs is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported_draft_type:{draft_type}",
        )
    _, preview_fn, _ = funcs

    suggested_payload = (
        payload.modified_payload if payload and payload.modified_payload else _parse_payload(audit)
    )

    try:
        result = preview_fn(db, suggested_payload)
        return result
    except Exception as exc:
        logger.exception("preview_draft failed for audit %s", audit_id)
        raise HTTPException(status_code=500, detail=f"preview_failed:{exc}") from exc


@router.post("/ai/drafts/{audit_id}/confirm")
def confirm_draft(
    audit_id: int,
    payload: ConfirmRequest | None = None,
    db: Session = Depends(get_db),
):
    """确认草案（user_confirmed=True）。

    可携带 modified_payload：用户在前端修改了字段后，将修改后的 payload 一并提交。
    - 原始 AI 建议保存在 preview_result.original_suggested_payload 中（用于差异追溯）
    - suggested_payload 更新为修改后的版本（execute 时使用）
    - 不传 modified_payload 则仅标记确认，不修改 payload

    关键约束：
    - 已拒绝的草案可重新确认（覆盖 rejected_reason）
    - 已执行的草案不可重新确认（返回 409）
    """
    audit = _load_audit(db, audit_id)

    # 已执行（有 final_result 且 user_confirmed）不可重新确认
    if audit.user_confirmed and audit.final_result:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "already_executed",
                "user_message": "该草案已执行，不可重新确认",
                "retryable": False,
            },
        )

    modified_payload = payload.modified_payload if payload else None

    # 若携带修改后的 payload，保存原始建议到 preview_result，更新 suggested_payload
    if modified_payload is not None:
        original_payload = _parse_payload(audit)
        # 保留既有的 preview 信息（如 dry-run 结果），追加原始建议
        existing_preview: dict[str, Any] = {}
        if audit.preview_result:
            try:
                existing_preview = json.loads(audit.preview_result)
                if not isinstance(existing_preview, dict):
                    existing_preview = {}
            except (ValueError, TypeError):
                existing_preview = {}
        existing_preview["original_suggested_payload"] = original_payload
        audit.preview_result = json.dumps(existing_preview, ensure_ascii=False)
        audit.suggested_payload = json.dumps(modified_payload, ensure_ascii=False)

    try:
        audit = confirm_action(db, audit_id, final_result=None)
        db.refresh(audit)
        return _audit_to_dict(audit)
    except ActionAuditNotFoundError as exc:
        raise HTTPException(status_code=404, detail="audit_not_found") from exc


@router.post("/ai/drafts/{audit_id}/reject")
def reject_draft(
    audit_id: int,
    payload: RejectRequest | None = None,
    db: Session = Depends(get_db),
):
    """拒绝草案（user_confirmed=False，记录原因）。

    关键约束：
    - 已执行的草案不可拒绝（返回 409）
    - 已确认但未执行的草案可拒绝（覆盖确认）
    """
    audit = _load_audit(db, audit_id)

    if audit.user_confirmed and audit.final_result:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "already_executed",
                "user_message": "该草案已执行，不可拒绝",
                "retryable": False,
            },
        )

    reason = payload.reason if payload else None
    try:
        audit = reject_action(db, audit_id, reason=reason)
        db.refresh(audit)
        return _audit_to_dict(audit)
    except ActionAuditNotFoundError as exc:
        raise HTTPException(status_code=404, detail="audit_not_found") from exc


@router.post("/ai/drafts/{audit_id}/execute")
def execute_draft(audit_id: int, db: Session = Depends(get_db)):
    """执行已确认草案。

    关键约束：
    - 未确认时拒绝执行（返回 409 error_code=not_confirmed）
    - 已执行（final_result 存在且成功）幂等返回既有结果
    - 调用 DRAFT_REGISTRY 中对应的 execute_xxx 函数
    - 只创建 draft 状态，不自动 transition/activate
    """
    audit = _load_audit(db, audit_id)
    draft_type = audit.action_type
    funcs = get_draft_functions(draft_type)
    if funcs is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported_draft_type:{draft_type}",
        )
    _, _, execute_fn = funcs

    # 未确认拒绝执行
    if not audit.user_confirmed:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "not_confirmed",
                "user_message": "用户未确认，拒绝执行",
                "retryable": True,
            },
        )

    # 幂等：已执行成功则直接返回既有结果
    if audit.final_result:
        try:
            existing = json.loads(audit.final_result)
            if isinstance(existing, dict) and existing.get("success"):
                return existing
        except (ValueError, TypeError):
            pass

    try:
        result = execute_fn(db, audit_id)
        return result
    except Exception as exc:
        logger.exception("execute_draft failed for audit %s", audit_id)
        raise HTTPException(status_code=500, detail=f"execute_failed:{exc}") from exc


__all__ = ["router"]
