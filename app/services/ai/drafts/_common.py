"""草稿工具共享逻辑（WP-AI.5）。

提供：
- load_audit_payload：加载审计记录并解析 payload（未确认时拒绝执行）
- build_draft_result：构造统一草稿返回结构
- build_preview_result：构造统一预览返回结构
- 字段校验工具
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── 统一返回结构构造 ────────────────────────────────────────


def build_draft_result(
    draft_type: str,
    suggested_payload: dict[str, Any],
    validation_status: str,  # "valid" | "invalid"
    validation_errors: list[str] | None = None,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造第一步（AI 建议）的统一返回结构。

    Args:
        draft_type: 草稿类型
        suggested_payload: AI 建议的 payload
        validation_status: 校验状态 "valid" | "invalid"
        validation_errors: 校验错误列表
        preview: 变更预览（来自 preview_xxx）

    Returns:
        {
            "draft_type": str,
            "suggested_payload": dict,
            "validation_status": "valid" | "invalid",
            "validation_errors": [...],
            "preview": {...},
            "requires_confirmation": True,  # 必须用户确认
        }
    """
    return {
        "draft_type": draft_type,
        "suggested_payload": suggested_payload,
        "validation_status": validation_status,
        "validation_errors": validation_errors or [],
        "preview": preview or {},
        "requires_confirmation": True,  # 关键：必须用户确认
    }


def build_preview_result(
    is_valid: bool,
    errors: list[str] | None = None,
    changes: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造第二步（系统规则校验和变更预览）的统一返回结构。

    Args:
        is_valid: 校验是否通过
        errors: 校验错误列表
        changes: 变更预览列表，每项 {field, old_value, new_value, description}
        extra: 额外信息（如订单草稿的风控检查详情）

    Returns:
        {
            "is_valid": bool,
            "errors": [...],
            "changes": [...],
            "extra": {...},
        }
    """
    result: dict[str, Any] = {
        "is_valid": is_valid,
        "errors": errors or [],
        "changes": changes or [],
    }
    if extra:
        result["extra"] = extra
    return result


# ── 审计记录加载与校验 ──────────────────────────────────────


def load_audit_payload(
    db: Session,
    audit_id: int,
    expected_action_type: str | None = None,
) -> tuple[Any, dict[str, Any], str | None]:
    """加载审计记录并解析 payload。

    Args:
        db: 数据库会话
        audit_id: AIActionAudit.id
        expected_action_type: 期望的 action_type，不匹配时拒绝

    Returns:
        (audit, payload, error)
        - 成功：(audit_obj, payload_dict, None)
        - 失败：(None, {}, error_message)

    关键约束：
    - audit 不存在 → error="audit_not_found"
    - audit.user_confirmed=False → error="not_confirmed"（不产生任何 DB 变化）
    - action_type 不匹配 → error="action_type_mismatch"
    """
    from app.services.ai_session_service import get_action_audit

    audit = get_action_audit(db, audit_id)
    if audit is None:
        return None, {}, "audit_not_found"

    # 关键：未确认时不执行任何 DB 变化
    if not audit.user_confirmed:
        return audit, {}, "not_confirmed"

    if expected_action_type and audit.action_type != expected_action_type:
        return audit, {}, "action_type_mismatch"

    # 解析 payload
    try:
        payload = json.loads(audit.suggested_payload)
        if not isinstance(payload, dict):
            payload = {}
    except (ValueError, TypeError):
        payload = {}

    return audit, payload, None


def record_final_result(db: Session, audit_id: int, result: dict[str, Any]) -> None:
    """记录最终执行结果到审计记录（best-effort）。"""
    try:
        from app.services.ai_session_service import confirm_action
        confirm_action(db, audit_id, final_result=result)
    except Exception as exc:
        logger.warning(
            "record_final_result failed for audit %s: %s", audit_id, exc
        )


# ── 字段校验工具 ────────────────────────────────────────────


def require_fields(payload: dict[str, Any], fields: list[str]) -> list[str]:
    """校验必填字段，返回缺失字段错误列表。"""
    errors: list[str] = []
    for f in fields:
        if f not in payload or payload[f] is None or payload[f] == "":
            errors.append(f"缺少必填字段: {f}")
    return errors


def validate_str_field(
    payload: dict[str, Any], field: str, max_len: int = 255,
) -> list[str]:
    """校验字符串字段长度。"""
    errors: list[str] = []
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        errors.append(f"字段 {field} 必须是字符串")
    elif isinstance(value, str) and len(value) > max_len:
        errors.append(f"字段 {field} 长度超过 {max_len}")
    return errors


def validate_number_field(
    payload: dict[str, Any], field: str,
    min_value: float | None = None, max_value: float | None = None,
) -> list[str]:
    """校验数字字段范围。"""
    errors: list[str] = []
    value = payload.get(field)
    if value is None:
        return errors
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"字段 {field} 必须是数字")
        return errors
    if min_value is not None and value < min_value:
        errors.append(f"字段 {field} 不能小于 {min_value}")
    if max_value is not None and value > max_value:
        errors.append(f"字段 {field} 不能大于 {max_value}")
    return errors


__all__ = [
    "build_draft_result",
    "build_preview_result",
    "load_audit_payload",
    "record_final_result",
    "require_fields",
    "validate_str_field",
    "validate_number_field",
]
