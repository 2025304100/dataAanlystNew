"""get_capabilities 工具：返回当前系统能力门禁和允许的下一步。"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack

logger = logging.getLogger(__name__)


def get_capabilities(db: Session, context: ContextPack) -> dict:
    """返回当前系统能力门禁和允许的下一步。

    Returns:
        {
            "status": "ok",
            "data": {
                "current_gate": "ready"|"degraded"|"blocked"|"unknown",
                "allowed_next_step": [...],
                "capabilities": [...],  # 各域详情
                "source_page": str,
            }
        }
        或 {"status": "no_data", "message": "..."}
    """
    try:
        from app.services.capability_gates import get_all_capabilities
        resp = get_all_capabilities(db)
    except Exception as exc:
        logger.warning("get_capabilities query failed: %s", exc)
        return {"status": "no_data", "message": f"能力门禁查询失败：{type(exc).__name__}"}

    # 直接复用 context.capabilities（已 best-effort 拉取过）
    cap_summary = context.capabilities if context.capabilities else {}

    return {
        "status": "ok",
        "data": {
            "current_gate": resp.overall_status,
            "allowed_next_step": cap_summary.get("allowed_next_step", ["explain"]),
            "capabilities": [
                {
                    "key": c.key,
                    "label": c.label,
                    "status": c.status,
                    "reason_code": c.reason_code,
                    "user_message": c.user_message,
                    "data_cutoff_at": c.data_cutoff_at.isoformat()
                    if c.data_cutoff_at else None,
                }
                for c in resp.capabilities
            ],
            "source_page": context.source_page,
            "checked_at": resp.checked_at.isoformat() if resp.checked_at else None,
        },
    }


__all__ = ["get_capabilities"]
