"""draft_note：观察备注/标签草稿（WP-AI.5）。

三步流程：
1. draft_note：AI 建议观察项备注/标签（不写 DB）
2. preview_note：系统校验观察项存在、字段合法（不写 DB）
3. execute_note：用户确认后更新 WatchlistItem 的 note/tags_json
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

DRAFT_TYPE = "draft_note"


def draft_note(
    db: Session, context: ContextPack, ai_suggestion: dict[str, Any]
) -> dict[str, Any]:
    """第一步：AI 建议观察项备注/标签。"""
    # 优先从 ai_suggestion 取 watchlist_item_id，其次从 context.references
    watchlist_item_id = (
        ai_suggestion.get("watchlist_item_id")
        or context.references.get("watchlist_item_id")
    )
    suggested_payload = {
        "watchlist_item_id": watchlist_item_id,
        "note": ai_suggestion.get("note", ""),
        "tags": ai_suggestion.get("tags", []),
        "priority": ai_suggestion.get("priority"),
    }

    preview = preview_note(db, suggested_payload)
    validation_status = "valid" if preview["is_valid"] else "invalid"
    validation_errors = preview["errors"]

    return build_draft_result(
        draft_type=DRAFT_TYPE,
        suggested_payload=suggested_payload,
        validation_status=validation_status,
        validation_errors=validation_errors,
        preview=preview,
    )


def preview_note(db: Session, suggested_payload: dict[str, Any]) -> dict[str, Any]:
    """第二步：系统校验和变更预览（dry-run）。"""
    errors: list[str] = []
    changes: list[dict[str, Any]] = []

    errors.extend(require_fields(suggested_payload, ["watchlist_item_id"]))

    item_id = suggested_payload.get("watchlist_item_id")
    old_values: dict[str, Any] = {}
    if item_id:
        try:
            from app.models.watchlist import WatchlistItem
            item = db.execute(
                select(WatchlistItem).where(WatchlistItem.id == item_id)
            ).scalars().first()
            if item is None:
                errors.append(f"观察项 watchlist_item_id={item_id} 不存在")
            else:
                old_values = {
                    "note": item.note,
                    "tags_json": item.tags_json,
                    "priority": item.priority,
                }
        except Exception as exc:
            logger.debug("preview_note item lookup failed: %s", exc)
            errors.append("观察项查询失败")

    # 字段长度
    errors.extend(validate_str_field(suggested_payload, "note", max_len=65535))

    # tags 结构校验
    tags = suggested_payload.get("tags", [])
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
            if not isinstance(parsed, list):
                errors.append("tags 必须是数组")
            else:
                suggested_payload["tags"] = parsed
        except (ValueError, TypeError):
            errors.append("tags 不是合法 JSON")
    elif not isinstance(tags, list):
        errors.append("tags 必须是数组或 JSON 字符串")

    changes.append({
        "field": "watchlist_items.note",
        "old_value": old_values.get("note"),
        "new_value": suggested_payload.get("note"),
        "description": f"更新观察项 {item_id} 的备注",
    })
    if suggested_payload.get("tags"):
        changes.append({
            "field": "watchlist_items.tags_json",
            "old_value": old_values.get("tags_json"),
            "new_value": suggested_payload.get("tags"),
            "description": f"更新观察项 {item_id} 的标签",
        })

    return build_preview_result(
        is_valid=len(errors) == 0, errors=errors, changes=changes,
    )


def execute_note(db: Session, audit_id: int) -> dict[str, Any]:
    """第三步：用户确认后执行（更新 WatchlistItem）。"""
    audit, payload, err = load_audit_payload(db, audit_id, expected_action_type=DRAFT_TYPE)
    if err == "audit_not_found":
        return {"success": False, "error": "audit_not_found", "message": "审计记录不存在"}
    if err == "not_confirmed":
        return {"success": False, "error": "not_confirmed", "message": "用户未确认，拒绝执行"}
    if err == "action_type_mismatch":
        return {"success": False, "error": "action_type_mismatch", "message": "动作类型不匹配"}

    try:
        from app.models.watchlist import WatchlistItem
        item_id = payload.get("watchlist_item_id")
        item = db.execute(
            select(WatchlistItem).where(WatchlistItem.id == item_id)
        ).scalars().first()
        if item is None:
            result = {"success": False, "error": "item_not_found", "message": f"观察项不存在: {item_id}"}
            record_final_result(db, audit_id, result)
            return result

        # 更新字段
        if "note" in payload:
            item.note = payload.get("note")
        tags = payload.get("tags")
        if tags is not None:
            if isinstance(tags, list):
                item.tags_json = json.dumps(tags, ensure_ascii=False)
            else:
                item.tags_json = tags
        if payload.get("priority") is not None:
            item.priority = int(payload.get("priority"))

        db.commit()
        db.refresh(item)

        result = {
            "success": True,
            "watchlist_item_id": item.id,
            "note": item.note,
            "message": f"观察项 {item.id} 已更新",
        }
        record_final_result(db, audit_id, result)
        return result
    except Exception as exc:
        db.rollback()
        logger.exception("execute_note failed")
        result = {"success": False, "error": "internal_error", "message": str(exc)}
        record_final_result(db, audit_id, result)
        return result


__all__ = ["draft_note", "preview_note", "execute_note"]
