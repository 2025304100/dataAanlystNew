"""draft_review：复盘草稿（WP-AI.5）。

三步流程：
1. draft_review：AI 建议组合复盘（不写 DB）
2. preview_review：系统校验组合存在、日期范围合法（不写 DB）
3. execute_review：用户确认后调用 Review CRUD 创建复盘记录（含归因快照）
"""
from __future__ import annotations

import logging
from datetime import date, datetime
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

DRAFT_TYPE = "draft_review"


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def draft_review(
    db: Session, context: ContextPack, ai_suggestion: dict[str, Any]
) -> dict[str, Any]:
    """第一步：AI 建议复盘。"""
    portfolio_id = (
        ai_suggestion.get("portfolio_id")
        or context.references.get("portfolio_id")
    )
    suggested_payload = {
        "portfolio_id": portfolio_id,
        "start_date": ai_suggestion.get("start_date"),
        "end_date": ai_suggestion.get("end_date"),
        "title": ai_suggestion.get("title", ""),
        "note": ai_suggestion.get("note", ""),
        "include_attribution_snapshot": ai_suggestion.get(
            "include_attribution_snapshot", True
        ),
    }

    preview = preview_review(db, suggested_payload)
    validation_status = "valid" if preview["is_valid"] else "invalid"
    validation_errors = preview["errors"]

    return build_draft_result(
        draft_type=DRAFT_TYPE,
        suggested_payload=suggested_payload,
        validation_status=validation_status,
        validation_errors=validation_errors,
        preview=preview,
    )


def preview_review(db: Session, suggested_payload: dict[str, Any]) -> dict[str, Any]:
    """第二步：系统校验和变更预览（dry-run）。"""
    errors: list[str] = []
    changes: list[dict[str, Any]] = []

    errors.extend(require_fields(
        suggested_payload, ["portfolio_id", "start_date", "end_date"]
    ))
    errors.extend(validate_str_field(suggested_payload, "title", max_len=255))
    errors.extend(validate_str_field(suggested_payload, "note", max_len=65535))

    # 日期解析与范围校验
    start_date = _parse_date(suggested_payload.get("start_date"))
    end_date = _parse_date(suggested_payload.get("end_date"))
    if suggested_payload.get("start_date") and start_date is None:
        errors.append("start_date 格式无效（应为 YYYY-MM-DD）")
    if suggested_payload.get("end_date") and end_date is None:
        errors.append("end_date 格式无效（应为 YYYY-MM-DD）")
    if start_date and end_date and start_date > end_date:
        errors.append("start_date 不能晚于 end_date")

    # 组合存在性校验
    portfolio_id = suggested_payload.get("portfolio_id")
    portfolio_exists = False
    if portfolio_id:
        try:
            from app.models.portfolio import Portfolio
            portfolio = db.execute(
                select(Portfolio).where(Portfolio.id == portfolio_id)
            ).scalars().first()
            if portfolio is None:
                errors.append(f"组合 portfolio_id={portfolio_id} 不存在")
            else:
                portfolio_exists = True
        except Exception as exc:
            logger.debug("preview_review portfolio lookup failed: %s", exc)
            errors.append("组合查询失败")

    changes.append({
        "field": "portfolio_reviews",
        "old_value": None,
        "new_value": {
            "portfolio_id": portfolio_id,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "title": suggested_payload.get("title"),
        },
        "description": f"为组合 {portfolio_id} 创建复盘记录",
    })

    return build_preview_result(
        is_valid=len(errors) == 0,
        errors=errors,
        changes=changes,
        extra={"portfolio_exists": portfolio_exists},
    )


def execute_review(db: Session, audit_id: int) -> dict[str, Any]:
    """第三步：用户确认后执行（调用 Review CRUD）。"""
    audit, payload, err = load_audit_payload(db, audit_id, expected_action_type=DRAFT_TYPE)
    if err == "audit_not_found":
        return {"success": False, "error": "audit_not_found", "message": "审计记录不存在"}
    if err == "not_confirmed":
        return {"success": False, "error": "not_confirmed", "message": "用户未确认，拒绝执行"}
    if err == "action_type_mismatch":
        return {"success": False, "error": "action_type_mismatch", "message": "动作类型不匹配"}

    try:
        from app.models.review import Review
        start_date = _parse_date(payload.get("start_date"))
        end_date = _parse_date(payload.get("end_date"))
        if start_date is None or end_date is None:
            result = {"success": False, "error": "invalid_date", "message": "日期格式无效"}
            record_final_result(db, audit_id, result)
            return result

        portfolio_id = payload.get("portfolio_id")
        # 再次校验组合存在
        from app.models.portfolio import Portfolio
        portfolio = db.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id)
        ).scalars().first()
        if portfolio is None:
            result = {"success": False, "error": "portfolio_not_found", "message": "组合不存在"}
            record_final_result(db, audit_id, result)
            return result

        # best-effort 生成归因快照
        report_snapshot_json = None
        if payload.get("include_attribution_snapshot", True):
            try:
                import json
                from app.services.attribution import get_attribution_report
                report = get_attribution_report(
                    db, portfolio_id, start_date=start_date, end_date=end_date,
                )
                report_snapshot_json = json.dumps(report, ensure_ascii=False, default=str)
            except Exception as exc:
                logger.warning("execute_review attribution snapshot failed: %s", exc)

        review = Review(
            portfolio_id=portfolio_id,
            start_date=start_date,
            end_date=end_date,
            report_snapshot_json=report_snapshot_json,
            note=payload.get("note", ""),
            title=payload.get("title", ""),
        )
        db.add(review)
        db.commit()
        db.refresh(review)

        result = {
            "success": True,
            "review_id": review.id,
            "portfolio_id": portfolio_id,
            "message": f"复盘记录已创建: {review.title or f'#{review.id}'}",
        }
        record_final_result(db, audit_id, result)
        return result
    except Exception as exc:
        db.rollback()
        logger.exception("execute_review failed")
        result = {"success": False, "error": "internal_error", "message": str(exc)}
        record_final_result(db, audit_id, result)
        return result


__all__ = ["draft_review", "preview_review", "execute_review"]
