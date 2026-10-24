"""One final human review, frozen to the current three prerequisite proofs."""
from __future__ import annotations
import json
from sqlalchemy import select
from app.models.portfolio import Portfolio
from app.services.portfolio_reconciliation_coverage import CoverageError, _hash
from app.services.portfolio_inspection_runtime import get_portfolio_runtime_inspection
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event


def confirm_inspection_guide(db, portfolio_id, *, expected_snapshot_id, expected_action,
        idempotency_key, review_note, actor):
    if len(review_note.strip()) < 10 or actor in {"system", "cron_worker"} or not actor.strip():
        raise CoverageError("PERSONAL_REVIEW_REQUIRED", "请本人填写至少10个字的审核理由", status_code=422)
    request_hash = _hash({"snapshot_id": expected_snapshot_id, "action": expected_action,
        "note": review_note.strip(), "actor": actor})
    with db.begin_nested():
        db.scalar(select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update())
        saved = db.scalar(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.portfolio_id == portfolio_id,
            DataGovernanceAuditEvent.correlation_id == idempotency_key,
            DataGovernanceAuditEvent.business_key == "inspection-guide-review").order_by(DataGovernanceAuditEvent.id.desc()).with_for_update())
        if saved:
            attrs = json.loads(saved.attributes_json)
            if attrs.get("request_hash") != request_hash:
                raise CoverageError("GUIDE_KEY_REUSED", "该审核键已用于不同材料或理由，请重新审核当前范围")
            return {**json.loads(saved.after_json), "audit_event_id": saved.id, "replayed": True}
        runtime = get_portfolio_runtime_inspection(db, portfolio_id, current_read=True)
        guide, summary = runtime["guide"], runtime["recovery_summary"]
        if expected_snapshot_id != guide["snapshot_id"] or expected_action != summary["confirmation_action"]:
            raise CoverageError("GUIDE_EVIDENCE_CHANGED", "步骤证明、报告、范围或运行条件已变化；保留审核理由，请重新检测并核实当前材料")
        if not summary["can_confirm"] or not guide["can_confirm"]:
            raise CoverageError("GUIDE_PREREQUISITES_NOT_MET", "前置步骤仍未通过或证据未知，不能提交恢复", details=summary["blockers"], status_code=428)
        if expected_action == "INITIAL_COVERAGE_REVIEW":
            from app.services.portfolio_reconciliation_activation import confirm_initial_coverage_review
            activation = summary["coverage_activation_review"]
            confirm_initial_coverage_review(db, portfolio_id, baseline_id=activation["baseline_id"], generation=activation["generation"],
                expected_cursor_row_version=activation["cursor_row_version"], expected_evidence_fingerprint=activation["evidence_fingerprint"],
                activation_key=idempotency_key + ":activate", acknowledge_new_scope=True, review_note=review_note.strip(), operator_id=actor)
        elif expected_action == "RECONCILIATION_CONFIRM":
            from app.services.portfolio_reconciliation import reconcile_trade_date
            from app.services.portfolio_state_machine import confirm_reconciliation_fixed
            proof = guide["steps"][0]["proof"]["check"]
            # The exact date is supplied by the scoped server proof, never a
            # client guess or the most recently clicked historical row.
            from datetime import date
            if not proof.get("trade_date") or not proof.get("report_id"):
                raise CoverageError("GUIDE_REPORT_MISSING", "当前步骤缺少确切交易日报告，请重新核查执行证据")
            target = date.fromisoformat(proof["trade_date"])
            report = reconcile_trade_date(db, portfolio_id, target, persist=False, current_read=True)
            confirm_reconciliation_fixed(db, portfolio_id, target, operator_id=actor, acknowledge_all_diffs_cleared=True,
                correlation_id=idempotency_key, review_note=review_note.strip(), report_id=report.report_id, report_version=report.report_version)
        else:
            from app.services.portfolio_state_machine import transition_portfolio_state
            transition_portfolio_state(db, portfolio_id, "READY", operator_id=actor, correlation_id=idempotency_key,
                reason="本人审核当前步骤证明后恢复", review_note=review_note.strip())
        refreshed = get_portfolio_runtime_inspection(db, portfolio_id, current_read=True)
        result = {"portfolio_id": portfolio_id, "current_state": refreshed["current_state"],
            "permissions": refreshed["permissions"], "trading_resumed": refreshed["permissions"]["allow_new_buys"] is True,
            "historical_unknown_retained": guide["effects"]["historical_unknown_retained"], "replayed": False}
        event = write_audit_event(db, "DATA_BLOCK_RESOLUTION", portfolio_id=portfolio_id,
            business_key="inspection-guide-review", operator_id=actor, correlation_id=idempotency_key,
            before={"snapshot_id": guide["snapshot_id"], "completion_proofs": guide["completion_proofs"], "state": runtime["current_state"]},
            after=result, attributes={"confirmation_type": "INSPECTION_GUIDE_CONFIRMED", "actor_type": "human",
                "request_hash": request_hash, "snapshot_id": guide["snapshot_id"], "action": expected_action,
                "review_decision": "approved", "completion_proofs": guide["completion_proofs"], "effects": guide["effects"],
                "bypass": False, "review_note": review_note.strip(), "ack": True}, note=review_note.strip())
        return {**result, "audit_event_id": event.event_id}
