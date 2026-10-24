"""Append-only investigation notes, never execution evidence or coverage approval."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.models.portfolio import Portfolio
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services.portfolio_reconciliation_coverage import CoverageError, _hash


MATERIAL_LABELS = {
    "execution": "自动运行记录与决策明细",
    "schedule": "历史自动接管与调度配置",
    "positions": "历史持仓明细",
}
CONCLUSION_LABELS = {
    "FOUND_PENDING_VALIDATION": "已找到材料，待来源核验",
    "NOT_FOUND": "已查找，材料仍缺失",
    "UNRECOVERABLE": "已说明暂无法恢复，历史仍待核实",
}


def _entry(row):
    value = json.loads(row.after_json)
    return {**value, "id": row.id, "created_at": row.occurred_at.isoformat(), "created_by": row.operator_id}


def list_material_investigations(db, portfolio_id: int, trade_date: date, material: str):
    prefix = f"material:{trade_date}:{material}:"
    rows = db.scalars(select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.portfolio_id == portfolio_id,
        DataGovernanceAuditEvent.action == "UNKNOWN_AUDIT_ACTION",
        DataGovernanceAuditEvent.business_key.startswith(prefix),
    ).order_by(DataGovernanceAuditEvent.id.desc()).limit(51)).all()
    return {"portfolio_id": portfolio_id, "trade_date": trade_date.isoformat(), "material": material,
            "items": [_entry(row) for row in rows[:50]], "has_more": len(rows) > 50, "read_only": True}


def record_material_investigation(db, portfolio_id: int, *, actor: str, **payload):
    # Serialize idempotent submissions for a portfolio without changing its state.
    if db.scalar(select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update()) is None:
        raise CoverageError("PORTFOLIO_NOT_FOUND", "当前组合不存在", status_code=404)
    trade_date, material = payload["trade_date"], payload["material"]
    body = {key: value for key, value in payload.items() if key != "request_key"}
    body["trade_date"] = trade_date.isoformat()
    request_hash = _hash({**body, "actor": actor})
    business_key = f"material:{trade_date}:{material}:{payload['request_key']}"
    existing = db.scalar(select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.portfolio_id == portfolio_id,
        DataGovernanceAuditEvent.business_key == business_key))
    if existing:
        attrs = json.loads(existing.attributes_json)
        if attrs.get("request_hash") != request_hash:
            raise CoverageError("MATERIAL_NOTE_CONFLICT", "这次登记已保存且内容不同，请读取原记录后重新登记", status_code=409)
        return {"portfolio_id": portfolio_id, "item": _entry(existing), "replayed": True}

    from app.services.portfolio_reconciliation import reconcile_trade_date
    report = reconcile_trade_date(db, portfolio_id, trade_date, persist=False)
    if report.report_id != body["report_ref"] or report.report_version != body["report_version"]:
        raise CoverageError("MATERIAL_REPORT_CHANGED", "核对材料已变化，请保留填写内容，刷新本日来源后重新登记", status_code=409)
    result = {**body, "material_label": MATERIAL_LABELS[material],
        "conclusion_label": CONCLUSION_LABELS[body["conclusion"]],
        "proves_execution": False, "reconciliation_passed": False, "trading_resumed": False,
        "baseline_accepted": False, "requires_source_validation": True}
    event = write_audit_event(db, "UNKNOWN_AUDIT_ACTION", portfolio_id=portfolio_id,
        business_key=business_key, operator_id=actor, occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
        after=result, note=f"{MATERIAL_LABELS[material]}：{CONCLUSION_LABELS[body['conclusion']]}。{body['note']}",
        attributes={"original_action": "INSPECTION_MATERIAL_INVESTIGATED", "source_timezone": "UTC",
            "actor_type": "human", "request_hash": request_hash, "trade_date": trade_date.isoformat(),
            "material": material, "report_ref": body["report_ref"], "report_version": body["report_version"],
            "execution_result": "not_executed", "review_decision": "not_applicable",
            "objects": [{"object_id": f"material:{material}:{trade_date}", "trade_date": trade_date.isoformat(),
                         "result": "unknown"}]})
    row = db.get(DataGovernanceAuditEvent, event.event_id)
    return {"portfolio_id": portfolio_id, "item": _entry(row), "replayed": False}
