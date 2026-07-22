"""draft_alert：告警规则草稿（WP-AI.5）。

三步流程：
1. draft_alert：AI 建议告警规则（不写 DB）
2. preview_alert：系统校验告警字段（不写 DB）
3. execute_alert：用户确认后调用 AlertRule CRUD 创建告警规则
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack
from app.services.ai.drafts._common import (
    build_draft_result,
    build_preview_result,
    load_audit_payload,
    record_final_result,
    require_fields,
    validate_number_field,
    validate_str_field,
)

logger = logging.getLogger(__name__)

DRAFT_TYPE = "draft_alert"

_VALID_ALERT_TYPES = (
    "score_drop", "data_stale", "indicator_trigger",
    "task_failed", "watchlist_signal", "price_alert",
)
_VALID_SEVERITIES = ("info", "warn", "error")


def draft_alert(
    db: Session, context: ContextPack, ai_suggestion: dict[str, Any]
) -> dict[str, Any]:
    """第一步：AI 建议告警规则。"""
    suggested_payload = {
        "name": ai_suggestion.get("name", ""),
        "alert_type": ai_suggestion.get("alert_type", "price_alert"),
        "severity": ai_suggestion.get("severity", "warn"),
        "config_json": ai_suggestion.get("config_json", "{}"),
        "cooldown_minutes": ai_suggestion.get("cooldown_minutes", 60),
        "enabled": ai_suggestion.get("enabled", True),
    }

    preview = preview_alert(db, suggested_payload)
    validation_status = "valid" if preview["is_valid"] else "invalid"
    validation_errors = preview["errors"]

    return build_draft_result(
        draft_type=DRAFT_TYPE,
        suggested_payload=suggested_payload,
        validation_status=validation_status,
        validation_errors=validation_errors,
        preview=preview,
    )


def preview_alert(db: Session, suggested_payload: dict[str, Any]) -> dict[str, Any]:
    """第二步：系统校验和变更预览（dry-run）。"""
    errors: list[str] = []
    changes: list[dict[str, Any]] = []

    errors.extend(require_fields(suggested_payload, ["name", "alert_type"]))
    errors.extend(validate_str_field(suggested_payload, "name", max_len=120))

    alert_type = suggested_payload.get("alert_type")
    if alert_type and alert_type not in _VALID_ALERT_TYPES:
        errors.append(f"alert_type 必须是 {_VALID_ALERT_TYPES} 之一")

    severity = suggested_payload.get("severity")
    if severity and severity not in _VALID_SEVERITIES:
        errors.append(f"severity 必须是 {_VALID_SEVERITIES} 之一")

    errors.extend(validate_number_field(
        suggested_payload, "cooldown_minutes", min_value=0, max_value=10080,
    ))

    # config_json 结构校验
    config_json = suggested_payload.get("config_json", "{}")
    if isinstance(config_json, str):
        try:
            parsed = json.loads(config_json)
            if not isinstance(parsed, dict):
                errors.append("config_json 必须是 JSON 对象")
        except (ValueError, TypeError):
            errors.append("config_json 不是合法 JSON")
    elif not isinstance(config_json, dict):
        errors.append("config_json 必须是 dict 或 JSON 字符串")

    changes.append({
        "field": "alert_rules",
        "old_value": None,
        "new_value": {"name": suggested_payload.get("name"), "alert_type": alert_type},
        "description": f"新增告警规则: {suggested_payload.get('name') or '(unnamed)'}",
    })

    return build_preview_result(
        is_valid=len(errors) == 0, errors=errors, changes=changes,
    )


def execute_alert(db: Session, audit_id: int) -> dict[str, Any]:
    """第三步：用户确认后执行（调用 AlertRule CRUD）。"""
    audit, payload, err = load_audit_payload(db, audit_id, expected_action_type=DRAFT_TYPE)
    if err == "audit_not_found":
        return {"success": False, "error": "audit_not_found", "message": "审计记录不存在"}
    if err == "not_confirmed":
        return {"success": False, "error": "not_confirmed", "message": "用户未确认，拒绝执行"}
    if err == "action_type_mismatch":
        return {"success": False, "error": "action_type_mismatch", "message": "动作类型不匹配"}

    try:
        from app.models.alert import AlertRule
        config_json = payload.get("config_json", "{}")
        if isinstance(config_json, dict):
            config_json = json.dumps(config_json, ensure_ascii=False)

        rule = AlertRule(
            name=payload.get("name", ""),
            alert_type=payload.get("alert_type", "price_alert"),
            enabled=1 if payload.get("enabled", True) else 0,
            severity=payload.get("severity", "warn"),
            config_json=config_json,
            cooldown_minutes=int(payload.get("cooldown_minutes", 60)),
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)

        result = {
            "success": True,
            "rule_id": rule.id,
            "name": rule.name,
            "message": f"告警规则已创建: {rule.name}",
        }
        record_final_result(db, audit_id, result)
        return result
    except Exception as exc:
        db.rollback()
        logger.exception("execute_alert failed")
        result = {"success": False, "error": "internal_error", "message": str(exc)}
        record_final_result(db, audit_id, result)
        return result


__all__ = ["draft_alert", "preview_alert", "execute_alert"]
