"""draft_indicator：自定义指标草稿（WP-AI.5）。

三步流程：
1. draft_indicator：AI 建议指标草稿（不写 DB）
2. preview_indicator：系统校验指标字段、检查重名（不写 DB）
3. execute_indicator：用户确认后调用 CustomIndicator CRUD 创建指标

业务约束：
- name/key 唯一
- formula 非空
- value_type 必须是 boolean/number/string 之一
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack
from app.services.ai.drafts._common import (
    build_draft_result,
    build_preview_result,
    load_audit_payload,
    record_final_result,
    require_fields,
    validate_str_field,
)

logger = logging.getLogger(__name__)

DRAFT_TYPE = "draft_indicator"

# 合法 value_type 取值
_VALID_VALUE_TYPES = ("boolean", "number", "string")
# 合法 scope 取值
_VALID_SCOPES = ("backtest", "discovery", "portfolio", "research")


def draft_indicator(
    db: Session, context: ContextPack, ai_suggestion: dict[str, Any]
) -> dict[str, Any]:
    """第一步：AI 建议指标草稿。"""
    # 提取建议字段（允许 AI 部分缺失，由 preview 校验）
    suggested_payload = {
        "name": ai_suggestion.get("name", ""),
        "key": ai_suggestion.get("key", ""),
        "description": ai_suggestion.get("description", ""),
        "category": ai_suggestion.get("category", "custom"),
        "formula": ai_suggestion.get("formula", ""),
        "value_type": ai_suggestion.get("value_type", "boolean"),
        "params_json": ai_suggestion.get("params_json", "[]"),
        "scope_json": ai_suggestion.get("scope_json", '["backtest", "discovery"]'),
        "enabled": ai_suggestion.get("enabled", True),
    }

    # 第二步：系统校验和预览
    preview = preview_indicator(db, suggested_payload)
    validation_status = "valid" if preview["is_valid"] else "invalid"
    validation_errors = preview["errors"]

    return build_draft_result(
        draft_type=DRAFT_TYPE,
        suggested_payload=suggested_payload,
        validation_status=validation_status,
        validation_errors=validation_errors,
        preview=preview,
    )


def preview_indicator(db: Session, suggested_payload: dict[str, Any]) -> dict[str, Any]:
    """第二步：系统校验和变更预览（dry-run，不写 DB）。"""
    errors: list[str] = []
    changes: list[dict[str, Any]] = []

    # 必填字段
    errors.extend(require_fields(suggested_payload, ["name", "key", "formula"]))

    # 字段类型与长度
    errors.extend(validate_str_field(suggested_payload, "name", max_len=128))
    errors.extend(validate_str_field(suggested_payload, "key", max_len=96))
    errors.extend(validate_str_field(suggested_payload, "formula", max_len=65535))

    # value_type 合法性
    value_type = suggested_payload.get("value_type")
    if value_type and value_type not in _VALID_VALUE_TYPES:
        errors.append(f"value_type 必须是 {_VALID_VALUE_TYPES} 之一")

    # scope 合法性
    scope_json = suggested_payload.get("scope_json", "[]")
    if isinstance(scope_json, str):
        try:
            import json
            scopes = json.loads(scope_json)
            if not isinstance(scopes, list):
                errors.append("scope_json 必须是数组")
            else:
                invalid = [s for s in scopes if s not in _VALID_SCOPES]
                if invalid:
                    errors.append(f"scope_json 含非法值: {invalid}")
        except (ValueError, TypeError):
            errors.append("scope_json 不是合法 JSON")
    elif not isinstance(scope_json, list):
        errors.append("scope_json 必须是数组或 JSON 字符串")

    # 检查重名（best-effort）
    name = suggested_payload.get("name")
    key = suggested_payload.get("key")
    if name or key:
        try:
            from app.models.custom_indicator import CustomIndicator
            if name:
                exists = db.execute(
                    select(CustomIndicator.id).where(CustomIndicator.name == name)
                ).scalar_one_or_none()
                if exists is not None:
                    errors.append(f"指标名称已存在: {name}")
            if key:
                exists = db.execute(
                    select(CustomIndicator.id).where(CustomIndicator.key == key)
                ).scalar_one_or_none()
                if exists is not None:
                    errors.append(f"指标 key 已存在: {key}")
        except Exception as exc:
            logger.debug("preview_indicator duplicate check failed: %s", exc)

    # 变更预览
    changes.append({
        "field": "custom_indicators",
        "old_value": None,
        "new_value": {
            "name": name,
            "key": key,
            "value_type": value_type,
        },
        "description": f"新增自定义指标: {name or '(unnamed)'}",
    })

    return build_preview_result(
        is_valid=len(errors) == 0,
        errors=errors,
        changes=changes,
    )


def execute_indicator(db: Session, audit_id: int) -> dict[str, Any]:
    """第三步：用户确认后执行（调用 CustomIndicator CRUD）。"""
    audit, payload, err = load_audit_payload(db, audit_id, expected_action_type=DRAFT_TYPE)
    if err == "audit_not_found":
        return {"success": False, "error": "audit_not_found", "message": "审计记录不存在"}
    if err == "not_confirmed":
        # 关键：未确认时不产生任何 DB 变化
        return {"success": False, "error": "not_confirmed", "message": "用户未确认，拒绝执行"}
    if err == "action_type_mismatch":
        return {"success": False, "error": "action_type_mismatch", "message": "动作类型不匹配"}

    try:
        from app.models.custom_indicator import CustomIndicator
        # 再次校验重名（防止确认期间被其他流程创建）
        name = payload.get("name")
        key = payload.get("key")
        if name:
            exists = db.execute(
                select(CustomIndicator.id).where(CustomIndicator.name == name)
            ).scalar_one_or_none()
            if exists is not None:
                result = {"success": False, "error": "duplicate_name", "message": f"指标名称已存在: {name}"}
                record_final_result(db, audit_id, result)
                return result

        indicator = CustomIndicator(
            name=name,
            key=key,
            description=payload.get("description", ""),
            category=payload.get("category", "custom"),
            formula=payload.get("formula", ""),
            value_type=payload.get("value_type", "boolean"),
            params_json=payload.get("params_json", "[]"),
            scope_json=payload.get("scope_json", '["backtest", "discovery"]'),
            enabled=bool(payload.get("enabled", True)),
        )
        db.add(indicator)
        db.commit()
        db.refresh(indicator)

        result = {
            "success": True,
            "indicator_id": indicator.id,
            "name": indicator.name,
            "key": indicator.key,
            "message": f"自定义指标已创建: {indicator.name}",
        }
        record_final_result(db, audit_id, result)
        return result
    except Exception as exc:
        db.rollback()
        logger.exception("execute_indicator failed")
        result = {"success": False, "error": "internal_error", "message": str(exc)}
        record_final_result(db, audit_id, result)
        return result


__all__ = ["draft_indicator", "preview_indicator", "execute_indicator"]
