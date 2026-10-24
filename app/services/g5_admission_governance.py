"""Reviewed, append-only HF-07 controls for NEW admission only.

No function submits orders, changes portfolio runtime/source modes, rewrites a
G5 report, or commits the caller's transaction. Missing deployment storage and
an unreviewed installation are frozen by default.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.g5_admission_governance import G5AdmissionControl, G5AdmissionEvent
from app.models.g5_dual_run import G5DualRunReport
from app.services.data_governance_audit import write_audit_event

TABLES = {"g5_admission_controls", "g5_admission_events"}


class G5GovernanceError(ValueError):
    def __init__(self, code: str, message: str, *, details=None, status_code=409):
        super().__init__(f"{code}：{message}")
        self.code, self.details, self.status_code = code, details, status_code


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _installed(db):
    return TABLES.issubset(inspect(db.connection()).get_table_names())


def _scope(portfolio_id):
    return "global" if portfolio_id is None else f"portfolio:{int(portfolio_id)}"


def _latest_report_event(db, report_id, *, current_read=False):
    statement = select(G5AdmissionEvent).where(G5AdmissionEvent.report_id == report_id,
        G5AdmissionEvent.event_type.in_(["INVALIDATE_REPORT", "RESTORE_REPORT"])).order_by(G5AdmissionEvent.id.desc()).limit(1)
    return db.scalar(statement.with_for_update().execution_options(populate_existing=True) if current_read else statement)


def report_revocation_reason(db: Session, record: G5DualRunReport, *, current_read: bool = False) -> str | None:
    """The single report-validator hook; freeze is a separate admission layer."""
    with db.no_autoflush:
        try:
            if not _installed(db):
                return "G5_GOVERNANCE_STORAGE_UNAVAILABLE：报告失效记录存储未部署，不能核验新准入证据"
            event = _latest_report_event(db, record.id, current_read=current_read) if record.id is not None else None
            if event is None:
                return None
            if event.original_report_hash != record.report_hash:
                return "G5_REPORT_IDENTITY_CHANGED：报告原哈希与追加处置记录不一致，需核查原件"
            if event.event_type == "INVALIDATE_REPORT":
                return f"G5_REPORT_INVALIDATED：报告已追加隔离（事件 {event.id}），仅限制新准入；{event.review_note}"
            return None
        except SQLAlchemyError:
            return "G5_GOVERNANCE_STORAGE_UNAVAILABLE：报告失效记录读取失败，不能确认新准入"


def _scope_state(db, key, *, missing_frozen, current_read=False):
    head = db.get(G5AdmissionControl, key, with_for_update=current_read, populate_existing=current_read)
    statement = select(G5AdmissionEvent).where(G5AdmissionEvent.scope_key == key).order_by(G5AdmissionEvent.id.desc())
    events = list(db.scalars(statement.with_for_update().execution_options(populate_existing=True) if current_read else statement))
    if (head is None and events) or (head is not None and head.last_event_id != (events[0].id if events else None)):
        return {"scope_key": key, "frozen": True, "version": head.row_version if head else 0,
                "reason": "G5_CONTROL_INCONSISTENT：治理游标与追加记录不一致", "event_id": None}
    decision = next((event for event in events if event.event_type in {"FREEZE", "RELEASE"}), None)
    frozen = decision.event_type == "FREEZE" if decision else missing_frozen
    return {"scope_key": key, "frozen": frozen, "version": head.row_version if head else 0,
        "event_id": decision.id if decision else None,
        "reason": (decision.review_note if decision else "尚未审核遗留报告影响并恢复新准入" if frozen else "继承全局新准入控制")}


def get_g5_admission_state(db: Session, portfolio_id: int | None = None, *, current_read=False) -> dict[str, Any]:
    with db.no_autoflush:
        try:
            if not _installed(db):
                raise G5GovernanceError("G5_GOVERNANCE_STORAGE_UNAVAILABLE", "新准入治理存储尚未部署")
            global_state = _scope_state(db, "global", missing_frozen=True, current_read=current_read)
            scoped = _scope_state(db, _scope(portfolio_id), missing_frozen=False, current_read=current_read) if portfolio_id is not None else global_state
            frozen = global_state["frozen"] or scoped["frozen"]
            return {"portfolio_id": portfolio_id, "frozen": frozen, "new_admission_only": True,
                "affects_existing_runtime": False, "version": scoped["version"],
                "global_version": global_state["version"], "scope_version": scoped["version"],
                "storage_status": "available", "global_control": global_state, "portfolio_control": scoped if portfolio_id is not None else None,
                "reasons": list(dict.fromkeys(item["reason"] for item in (global_state, scoped) if item["frozen"])),
                "reason_codes": ["G5_NEW_ADMISSION_FROZEN"] if frozen else []}
        except (SQLAlchemyError, G5GovernanceError):
            return {"portfolio_id": portfolio_id, "frozen": True, "new_admission_only": True,
                "affects_existing_runtime": False, "version": 0, "global_version": 0, "scope_version": 0,
                "storage_status": "unavailable", "reasons": ["新准入治理存储不可读取，请先完成部署与只读影响盘点"],
                "reason_codes": ["G5_GOVERNANCE_STORAGE_UNAVAILABLE"]}


def assert_g5_admission_open_for_update(db: Session, portfolio_id: int) -> dict:
    """Serialize final new-admission writes with freezes; never creates a head."""
    state = get_g5_admission_state(db, portfolio_id, current_read=True)
    if state["frozen"]:
        raise G5GovernanceError(state["reason_codes"][0], "新的方案准入当前被冻结：" + "；".join(state["reasons"]))
    return state


def _personal(operator_id, note, request_key, acknowledge):
    if not operator_id or operator_id.strip() in {"system", "cron_worker"} or operator_id.startswith("system:"):
        raise G5GovernanceError("G5_PERSONAL_REVIEW_REQUIRED", "此处置需真实操作者审核，后台任务不能代签", status_code=403)
    if not note or not 10 <= len(note.strip()) <= 2000:
        raise G5GovernanceError("G5_REVIEW_NOTE_REQUIRED", "请填写至少10个有效字符的处置理由", status_code=422)
    if not request_key or not 8 <= len(request_key.strip()) <= 128:
        raise G5GovernanceError("G5_REQUEST_KEY_REQUIRED", "请提供有效的处置幂等键", status_code=422)
    if acknowledge is not True:
        raise G5GovernanceError("G5_NEW_ADMISSION_ACK_REQUIRED", "需明确本操作只限制新准入，不能自动处置现有交易", status_code=422)


def _head(db, key):
    row = db.scalar(select(G5AdmissionControl).where(G5AdmissionControl.scope_key == key).with_for_update().execution_options(populate_existing=True))
    if row is None:
        row = G5AdmissionControl(scope_key=key, row_version=0)
        db.add(row)
        db.flush()
    return row


def _replay(db, key, fingerprint):
    event = db.scalar(select(G5AdmissionEvent).where(G5AdmissionEvent.request_key == key).with_for_update())
    if event:
        if event.request_hash != fingerprint:
            raise G5GovernanceError("G5_REQUEST_KEY_REUSED", "相同幂等键已经用于不同审核内容")
        return {**json.loads(event.payload_json)["result"], "idempotent": True, "event_id": event.id, "audit_event_id": event.audit_event_id}
    return None


def _inventory(db, portfolio_id, expected):
    from app.services.g5_legacy_inventory import build_g5_legacy_inventory
    # Lock the report range before comparing the reviewed snapshot. New report
    # inserts cannot race a global release on databases with range locking.
    statement = select(G5DualRunReport).order_by(G5DualRunReport.id)
    if portfolio_id is not None:
        statement = statement.where(G5DualRunReport.portfolio_id == portfolio_id)
    list(db.scalars(statement.with_for_update().execution_options(populate_existing=True)))
    inventory = build_g5_legacy_inventory(db, portfolio_id=portfolio_id, current_read=True)
    if not inventory.get("reviewable") or inventory.get("storage_errors"):
        raise G5GovernanceError("G5_INVENTORY_UNAVAILABLE", "来源读取不完整，不能把空盘点当作审核完成", details=inventory.get("storage_errors"), status_code=503)
    if not expected or inventory["snapshot_id"] != expected:
        raise G5GovernanceError("G5_INVENTORY_CHANGED", "报告、采用引用或处置证据已变化，请重新读取影响盘点")
    return inventory


def _append(db, head, *, kind, portfolio_id, report, inventory, note, operator_id, key, fingerprint, request, result):
    before = {"head_event_id": head.last_event_id, "version": head.row_version}
    audit = write_audit_event(db, "DATA_BLOCK_RESOLUTION", portfolio_id=portfolio_id,
        business_key=f"g5-governance:{head.scope_key}", operator_id=operator_id, correlation_id=key,
        occurred_at=_now(), before=before, after=result,
        attributes={"confirmation_type": f"G5_ADMISSION_{kind}", "new_admission_only": True,
            "report_id": report.id if report else None, "original_report_hash": report.report_hash if report else None,
            "inventory_snapshot_id": inventory.get("snapshot_id") if inventory else None,
            "review_note": note.strip(), "ack": True}, note=note.strip())
    event = G5AdmissionEvent(scope_key=head.scope_key, event_type=kind, portfolio_id=portfolio_id,
        report_id=report.id if report else None, original_report_hash=report.report_hash if report else None,
        inventory_snapshot_id=inventory.get("snapshot_id") if inventory else None,
        review_note=note.strip(), operator_id=operator_id, request_key=key, request_hash=fingerprint,
        previous_event_id=head.last_event_id, audit_event_id=audit.event_id,
        payload_json=_canonical({"request": request, "result": result, "inventory": inventory,
            "rollback_material": {"previous_control": before, "original_report_id": report.id if report else None,
                "original_hash": report.report_hash if report else None,
                "recovery": "追加审核恢复事件；不可删除旧处置或重写原报告；不修改账户交易事实"}}))
    db.add(event)
    db.flush()
    head.last_event_id = event.id
    head.row_version += 1
    db.flush()
    return {**result, "event_id": event.id, "audit_event_id": audit.event_id, "version": head.row_version, "idempotent": False}


def set_g5_admission_freeze(db: Session, *, frozen: bool, expected_version: int,
        review_note: str, request_key: str, operator_id: str, portfolio_id: int | None = None,
        inventory_snapshot_id: str | None = None, acknowledge_only_new_admission: bool = False,
        impact_reviews: list[dict] | None = None) -> dict:
    _personal(operator_id, review_note, request_key, acknowledge_only_new_admission)
    if type(frozen) is not bool:
        raise G5GovernanceError("G5_FREEZE_VALUE_INVALID", "冻结状态必须为明确布尔值", status_code=422)
    if not _installed(db):
        raise G5GovernanceError("G5_GOVERNANCE_STORAGE_UNAVAILABLE", "请先部署新准入治理存储", status_code=503)
    request = {"portfolio_id": portfolio_id, "frozen": frozen, "expected_version": expected_version,
        "inventory_snapshot_id": inventory_snapshot_id, "review_note": review_note.strip(),
        "request_key": request_key, "operator_id": operator_id, "impact_reviews": impact_reviews or []}
    fingerprint = _hash(request)
    with db.begin_nested():
        global_head = _head(db, "global")
        previous = _replay(db, request_key, fingerprint)
        if previous:
            return previous
        if portfolio_id is not None:
            from app.models.portfolio import Portfolio
            if db.get(Portfolio, portfolio_id, with_for_update=True, populate_existing=True) is None:
                raise G5GovernanceError("PORTFOLIO_NOT_FOUND", "组合不存在，不能创建准入处置范围", status_code=404)
        head = global_head if portfolio_id is None else _head(db, _scope(portfolio_id))
        if head.row_version != expected_version:
            raise G5GovernanceError("G5_CONTROL_VERSION_CHANGED", "冻结或处置记录已变化，请重新读取审核材料")
        inventory = None
        if not frozen:
            inventory = _inventory(db, portfolio_id, inventory_snapshot_id)
            uncontained = []
            for item in inventory.get("reports", []):
                if item.get("evidence_verified"):
                    continue
                event = _latest_report_event(db, item["report_id"], current_read=True)
                if not event or event.event_type != "INVALIDATE_REPORT" or event.original_report_hash != item["original_hash"]:
                    uncontained.append(item["report_id"])
            if uncontained:
                raise G5GovernanceError("G5_INVALID_REPORTS_NOT_ISOLATED", "仍有无效遗留报告未按原哈希追加隔离", details=uncontained)
            required = {item["portfolio_id"] for item in inventory.get("affected_portfolios", [])}
            reviewed = {item.get("portfolio_id") for item in (impact_reviews or []) if item.get("disposition") in {
                "retain_existing_runtime", "requires_separate_review"} and len(str(item.get("rationale", "")).strip()) >= 10
                and item.get("evidence_refs") and all(str(ref).strip() for ref in item["evidence_refs"])}
            if not required <= reviewed:
                raise G5GovernanceError("G5_IMPACT_REVIEW_REQUIRED", "每个受影响组合都需独立影响说明、处置结论与证据引用", details=sorted(required - reviewed))
        effective_frozen = frozen or (portfolio_id is not None and _scope_state(db, "global", missing_frozen=True, current_read=True)["frozen"])
        result = {"portfolio_id": portfolio_id, "scope_key": head.scope_key,
            "frozen": effective_frozen, "scope_frozen": frozen,
            "version": head.row_version + 1, "new_admission_only": True,
            "existing_runtime_changed": False, "inventory_snapshot_id": inventory_snapshot_id,
            "impact_reviews": impact_reviews or []}
        return _append(db, head, kind="FREEZE" if frozen else "RELEASE", portfolio_id=portfolio_id,
            report=None, inventory=inventory, note=review_note, operator_id=operator_id,
            key=request_key, fingerprint=fingerprint, request=request, result=result)


def append_g5_report_invalidation(db: Session, report_id: int, *, action: str,
        expected_report_hash: str, inventory_snapshot_id: str, expected_version: int,
        review_note: str, request_key: str, operator_id: str,
        acknowledge_only_new_admission: bool = False) -> dict:
    _personal(operator_id, review_note, request_key, acknowledge_only_new_admission)
    if action not in {"invalidate", "restore"}:
        raise G5GovernanceError("G5_INVALIDATION_ACTION_INVALID", "处置类型必须为隔离或审核恢复", status_code=422)
    if not _installed(db):
        raise G5GovernanceError("G5_GOVERNANCE_STORAGE_UNAVAILABLE", "请先部署新准入治理存储", status_code=503)
    request = dict(report_id=report_id, action=action, expected_report_hash=expected_report_hash,
        inventory_snapshot_id=inventory_snapshot_id, expected_version=expected_version,
        review_note=review_note.strip(), request_key=request_key, operator_id=operator_id)
    fingerprint = _hash(request)
    with db.begin_nested():
        head = _head(db, "global")
        previous = _replay(db, request_key, fingerprint)
        if previous:
            return previous
        if head.row_version != expected_version:
            raise G5GovernanceError("G5_CONTROL_VERSION_CHANGED", "治理处置记录已变化，请重新读取")
        record = db.scalar(select(G5DualRunReport).where(G5DualRunReport.id == report_id).with_for_update().execution_options(populate_existing=True))
        if record is None:
            raise G5GovernanceError("G5_REPORT_NOT_FOUND", "报告不存在，不能生成隔离或恢复记录", status_code=404)
        if record.report_hash != expected_report_hash:
            raise G5GovernanceError("G5_REPORT_IDENTITY_CHANGED", "报告原件哈希已变化，请重新盘点来源")
        inventory = _inventory(db, record.portfolio_id, inventory_snapshot_id)
        if not any(item["report_id"] == report_id and item["original_hash"] == expected_report_hash for item in inventory["reports"]):
            raise G5GovernanceError("G5_REPORT_NOT_IN_INVENTORY", "此报告不属于已核对的组合盘点快照")
        if not get_g5_admission_state(db, record.portfolio_id, current_read=True)["frozen"]:
            raise G5GovernanceError("G5_FREEZE_REQUIRED", "请先冻结新的准入，再审核遗留报告隔离或恢复")
        if action == "restore":
            invalidation = _latest_report_event(db, report_id, current_read=True)
            if not invalidation or invalidation.event_type != "INVALIDATE_REPORT" or invalidation.original_report_hash != record.report_hash:
                raise G5GovernanceError("G5_ACTIVE_INVALIDATION_REQUIRED", "没有匹配原件哈希的有效隔离记录可供恢复")
            from app.services.g5_dual_run_audit import validate_g5_report
            checked = validate_g5_report(db, record, portfolio_id=record.portfolio_id, check_invalidation=False, current_read=True)
            if not checked.get("evidence_verified"):
                raise G5GovernanceError("G5_REPORT_STILL_INVALID", "原报告仍不具备真实证据，审核理由不能将其恢复为准入证据", details=checked.get("g5_validation_errors"))
        result = {"report_id": report_id, "portfolio_id": record.portfolio_id,
            "original_report_hash": expected_report_hash, "action": action,
            "new_admission_only": True, "existing_runtime_changed": False,
            "version": head.row_version + 1, "inventory_snapshot_id": inventory_snapshot_id}
        return _append(db, head, kind="INVALIDATE_REPORT" if action == "invalidate" else "RESTORE_REPORT",
            portfolio_id=record.portfolio_id, report=record, inventory=inventory,
            note=review_note, operator_id=operator_id, key=request_key,
            fingerprint=fingerprint, request=request, result=result)
