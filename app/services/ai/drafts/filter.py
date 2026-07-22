"""draft_filter：筛选方案草稿（WP-AI.5）。

三步流程：
1. draft_filter：AI 建议筛选方案（不写 DB）
2. preview_filter：系统校验筛选字段、检查重名（不写 DB）
3. execute_filter：用户确认后调用 ScanPreset CRUD 创建筛选方案
"""
from __future__ import annotations

import json
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

DRAFT_TYPE = "draft_filter"

_VALID_SCOPE_TYPES = ("universe", "portfolio", "watchlist")
_VALID_SORT_MODES = ("quality_desc", "timing_desc", "priority_desc", "score_desc", "rank_asc")


def draft_filter(
    db: Session, context: ContextPack, ai_suggestion: dict[str, Any]
) -> dict[str, Any]:
    """第一步：AI 建议筛选方案。"""
    suggested_payload = {
        "name": ai_suggestion.get("name", ""),
        "scope_type": ai_suggestion.get("scope_type", "universe"),
        "markets": ai_suggestion.get("markets", []),
        "boards": ai_suggestion.get("boards", []),
        "filters_json": ai_suggestion.get("filters_json", "[]"),
        "sort_mode": ai_suggestion.get("sort_mode", "priority_desc"),
        "description": ai_suggestion.get("description", ""),
    }

    preview = preview_filter(db, suggested_payload)
    validation_status = "valid" if preview["is_valid"] else "invalid"
    validation_errors = preview["errors"]

    return build_draft_result(
        draft_type=DRAFT_TYPE,
        suggested_payload=suggested_payload,
        validation_status=validation_status,
        validation_errors=validation_errors,
        preview=preview,
    )


def preview_filter(db: Session, suggested_payload: dict[str, Any]) -> dict[str, Any]:
    """第二步：系统校验和变更预览（dry-run）。"""
    errors: list[str] = []
    changes: list[dict[str, Any]] = []

    errors.extend(require_fields(suggested_payload, ["name", "scope_type"]))
    errors.extend(validate_str_field(suggested_payload, "name", max_len=128))

    scope_type = suggested_payload.get("scope_type")
    if scope_type and scope_type not in _VALID_SCOPE_TYPES:
        errors.append(f"scope_type 必须是 {_VALID_SCOPE_TYPES} 之一")

    sort_mode = suggested_payload.get("sort_mode")
    if sort_mode and sort_mode not in _VALID_SORT_MODES:
        errors.append(f"sort_mode 必须是 {_VALID_SORT_MODES} 之一")

    # filters_json 结构校验
    filters_json = suggested_payload.get("filters_json", "[]")
    if isinstance(filters_json, str):
        try:
            parsed = json.loads(filters_json)
            if not isinstance(parsed, list):
                errors.append("filters_json 必须是数组")
        except (ValueError, TypeError):
            errors.append("filters_json 不是合法 JSON")
    elif not isinstance(filters_json, list):
        errors.append("filters_json 必须是数组或 JSON 字符串")

    # 检查重名
    name = suggested_payload.get("name")
    if name:
        try:
            from app.models.scan import ScanPreset
            exists = db.execute(
                select(ScanPreset.id).where(ScanPreset.name == name)
            ).scalar_one_or_none()
            if exists is not None:
                errors.append(f"筛选方案名称已存在: {name}")
        except Exception as exc:
            logger.debug("preview_filter duplicate check failed: %s", exc)

    changes.append({
        "field": "scan_presets",
        "old_value": None,
        "new_value": {"name": name, "scope_type": scope_type},
        "description": f"新增筛选方案: {name or '(unnamed)'}",
    })

    return build_preview_result(
        is_valid=len(errors) == 0, errors=errors, changes=changes,
    )


def execute_filter(db: Session, audit_id: int) -> dict[str, Any]:
    """第三步：用户确认后执行（调用 ScanPreset CRUD）。"""
    audit, payload, err = load_audit_payload(db, audit_id, expected_action_type=DRAFT_TYPE)
    if err == "audit_not_found":
        return {"success": False, "error": "audit_not_found", "message": "审计记录不存在"}
    if err == "not_confirmed":
        return {"success": False, "error": "not_confirmed", "message": "用户未确认，拒绝执行"}
    if err == "action_type_mismatch":
        return {"success": False, "error": "action_type_mismatch", "message": "动作类型不匹配"}

    try:
        from app.models.scan import ScanPreset
        # 序列化 markets/boards 为 JSON 字符串
        markets = payload.get("markets", [])
        boards = payload.get("boards", [])
        markets_json = json.dumps(markets, ensure_ascii=False) if isinstance(markets, list) else (markets or "")
        boards_json = json.dumps(boards, ensure_ascii=False) if isinstance(boards, list) else (boards or "")
        filters_json = payload.get("filters_json", "[]")
        if isinstance(filters_json, list):
            filters_json = json.dumps(filters_json, ensure_ascii=False)

        # 再次校验重名
        name = payload.get("name")
        if name:
            exists = db.execute(
                select(ScanPreset.id).where(ScanPreset.name == name)
            ).scalar_one_or_none()
            if exists is not None:
                result = {"success": False, "error": "duplicate_name", "message": f"筛选方案名称已存在: {name}"}
                record_final_result(db, audit_id, result)
                return result

        preset = ScanPreset(
            name=name,
            scope_type=payload.get("scope_type", "universe"),
            markets=markets_json,
            boards=boards_json,
            filters_json=filters_json,
            sort_mode=payload.get("sort_mode"),
            description=payload.get("description", ""),
        )
        db.add(preset)
        db.commit()
        db.refresh(preset)

        result = {
            "success": True,
            "preset_id": preset.id,
            "name": preset.name,
            "message": f"筛选方案已创建: {preset.name}",
        }
        record_final_result(db, audit_id, result)
        return result
    except Exception as exc:
        db.rollback()
        logger.exception("execute_filter failed")
        result = {"success": False, "error": "internal_error", "message": str(exc)}
        record_final_result(db, audit_id, result)
        return result


__all__ = ["draft_filter", "preview_filter", "execute_filter"]
