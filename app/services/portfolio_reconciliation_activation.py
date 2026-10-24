"""Personal activation of an accepted, not-yet-executed coverage generation.

An accepted current-account baseline can authorize the first future execution
without manufacturing a historical reconciliation pass. The activation is a
separate audit fact, scoped to one immutable baseline and generation.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio, PORTFOLIO_STATUS_VALUES
from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationCursor
from app.services import portfolio_reconciliation_coverage as coverage
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services.portfolio_state_machine import VALID_STATES, _get_status, _set_status


ACTIVATION_TYPE = "INITIAL_COVERAGE_REVIEW_CONFIRMED"
CoverageError = coverage.CoverageError
_COVERAGE_TABLES = {"portfolio_account_baselines", "portfolio_reconciliation_cursors"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _block(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}


def _json(value: str | None) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _active(db: Session, portfolio_id: int, *, current_read: bool = False) -> dict | None:
    tables = set(inspect(db.connection()).get_table_names())
    present = tables & _COVERAGE_TABLES
    if not present:
        return None  # Legacy deployment with no coverage feature installed.
    if present != _COVERAGE_TABLES or "data_governance_audit_events" not in tables:
        raise CoverageError("COVERAGE_SOURCE_UNAVAILABLE", "覆盖基线、游标或本人接受审计来源不完整，请先修复来源")
    return coverage.get_active_coverage(db, portfolio_id, current_read=current_read)


def _activation_events(db: Session, portfolio_id: int, *, current_read: bool = False) -> list[DataGovernanceAuditEvent]:
    if "data_governance_audit_events" not in inspect(db.connection()).get_table_names():
        return []
    statement = select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.portfolio_id == portfolio_id,
        DataGovernanceAuditEvent.action == "DATA_BLOCK_RESOLUTION",
    ).order_by(DataGovernanceAuditEvent.id.desc()).execution_options(populate_existing=True)
    return list(db.scalars(statement.with_for_update() if current_read else statement))


def get_initial_coverage_activation(
    db: Session, portfolio_id: int, *, active: dict | None = None, current_read: bool = False,
) -> dict[str, Any] | None:
    """Read the matching activation fact, without revalidating later fills.

    Once execution begins, current source watermarks naturally change. That
    does not invalidate this audit fact; a different baseline/generation does.
    """
    with db.no_autoflush:
        active = active if active is not None else _active(db, portfolio_id, current_read=current_read)
        if active is None:
            return None
        for event in _activation_events(db, portfolio_id, current_read=current_read):
            attrs = _json(event.attributes_json)
            if (attrs.get("confirmation_type") == ACTIVATION_TYPE
                    and attrs.get("baseline_id") == active["baseline_id"]
                    and attrs.get("generation") == active["generation"]
                    and attrs.get("new_scope_start") == active["coverage_start_trade_date"].isoformat()
                    and attrs.get("ack") is True
                    and attrs.get("source_verified") is True
                    and event.business_key == active["baseline_id"]
                    and isinstance(attrs.get("activation_key"), str)
                    and bool(attrs["activation_key"].strip())
                    and attrs.get("activation_key") == event.correlation_id
                    and re.fullmatch(r"[a-f0-9]{64}", str(attrs.get("request_hash") or ""))
                    and event.operator_id and event.operator_id.strip() not in {"", "system", "cron_worker"}):
                return {"audit_id": event.id, "audit_event_id": event.id, "attributes": attrs,
                        "baseline_id": attrs["baseline_id"], "generation": attrs["generation"],
                        "activation_type": ACTIVATION_TYPE, "after": _json(event.after_json)}
        return None


def _calendar_check(db: Session, active: dict, *, current_read: bool) -> dict:
    """Compare the real accepted calendar segment, not a freshly chosen start."""
    try:
        trusted = coverage._trusted_calendar(db, coverage._utc(_now()), lock=current_read)
    except CoverageError as exc:
        return {"passed": False, "reason": str(exc), "version": None}
    inspector = inspect(db.connection())
    if "trade_calendar" not in inspector.get_table_names():
        return {"passed": False, "reason": "真实交易日历来源缺失", "version": None}
    table = Table("trade_calendar", MetaData(), autoload_with=db.connection())
    if not {"date", "is_trading_day"}.issubset(table.c.keys()):
        return {"passed": False, "reason": "交易日历来源字段不完整", "version": None}
    frozen_day = coverage._utc(active["baseline_as_of_at"]).replace(tzinfo=timezone.utc).astimezone(coverage.SHANGHAI).date()
    target = active["coverage_start_trade_date"]
    statement = select(table).where(table.c.date > frozen_day, table.c.date <= target).order_by(table.c.date)
    rows = list(db.execute(statement.with_for_update() if current_read else statement).mappings())
    frozen = [{"date": row["date"], "is_trading_day": row["is_trading_day"]} for row in rows]
    version = coverage._hash({"source_version": trusted["version"], "rows": frozen})
    dates = {row["date"] for row in rows}
    complete = all(frozen_day + timedelta(days=i) in dates for i in range(1, (target - frozen_day).days + 1))
    first_open = next((row["date"] for row in rows if row["is_trading_day"] == 1), None)
    passed = (complete and all(row["is_trading_day"] in {0, 1} for row in rows)
              and first_open == target and version == active["calendar_version"])
    return {"passed": passed, "reason": "新起点对应的真实日历与接受时一致" if passed else
            "新起点日历证据已变化或存在缺行，请重新建立可信基线", "version": version}


def _executions(db: Session, portfolio_id: int, active: dict, *, current_read: bool) -> list[str]:
    from app.models.decision_engine import DecisionRun
    statement = select(DecisionRun.id).where(DecisionRun.portfolio_id == portfolio_id,
        DecisionRun.run_type == "auto_simulation", DecisionRun.trade_date >= active["coverage_start_trade_date"])
    return list(db.scalars(statement.with_for_update() if current_read else statement))


def get_coverage_activation_review(db: Session, portfolio_id: int) -> dict[str, Any]:
    with db.no_autoflush:
        return _review(db, portfolio_id, current_read=False)


def _review(db: Session, portfolio_id: int, *, current_read: bool) -> dict[str, Any]:
    from app.services.portfolio_inspection_runtime import _current_binding_check, _current_data_check
    statement = select(Portfolio).where(Portfolio.id == portfolio_id).execution_options(populate_existing=True)
    p = db.scalar(statement.with_for_update() if current_read else statement)
    if p is None:
        raise CoverageError("PORTFOLIO_NOT_FOUND", "组合不存在", status_code=404)
    state = _get_status(db, p, current_read=current_read)
    dimensions = {key: getattr(getattr(p, key), "value", getattr(p, key)) for key in (
        "status_score", "status_data", "status_model", "status_reconciliation")}
    result: dict[str, Any] = {"portfolio_id": portfolio_id, "can_confirm": False, "blockers": [],
        "baseline_id": None, "generation": None, "cursor_row_version": None,
        "evidence_fingerprint": None, "coverage_start_trade_date": None,
        "already_activated": False, "source_refs": [], "checks": [], "current_state": state,
        "dimensions": dimensions, "activation_type": ACTIVATION_TYPE,
        "historical_coverage_unknown": True, "reconciliation_passed": False}
    try:
        active = _active(db, portfolio_id, current_read=current_read)
    except CoverageError as exc:
        result["blockers"].append(_block(exc.code, str(exc), exc.details))
        return result
    if active is None:
        result["blockers"].append(_block("ACCEPTED_COVERAGE_REQUIRED", "尚无可核验的本人接受基线，不能启用新覆盖范围"))
        return result
    result.update(baseline_id=active["baseline_id"], generation=active["generation"],
        cursor_row_version=active["row_version"], coverage_start_trade_date=active["coverage_start_trade_date"],
        source_refs=active["baseline_snapshot"].get("source_watermarks", []), history_gaps=active["history_gaps"])
    activation = get_initial_coverage_activation(db, portfolio_id, active=active, current_read=current_read)
    if activation:
        result.update(already_activated=True, audit_event_id=activation["audit_event_id"],
                      activation=activation, evidence_fingerprint=activation["attributes"].get("evidence_fingerprint"))
        result["blockers"].append(_block("INITIAL_COVERAGE_ALREADY_ACTIVATED", "本代新范围已审核启用，后续执行与对账沿用独立门禁"))
        return result
    blockers = result["blockers"]
    if state not in VALID_STATES or any(v not in PORTFOLIO_STATUS_VALUES for v in dimensions.values()):
        blockers.append(_block("RUNTIME_STATE_UNKNOWN", "当前状态或独立门禁值未知，不能审核启用"))
    try:
        source_check = coverage.revalidate_active_coverage_sources(db, portfolio_id,
            expected_baseline_id=active["baseline_id"], current_read=current_read)
        result["source_refs"] = source_check["current_source_refs"]
        result["checks"].append({"key": "account_sources", "passed": True, "reason": "账户、成本和未决义务与本人接受快照一致"})
    except CoverageError as exc:
        blockers.append(_block(exc.code, str(exc), exc.details))
        result["checks"].append({"key": "account_sources", "passed": False, "reason": str(exc)})
    now = coverage._utc(_now())
    local_today = now.replace(tzinfo=timezone.utc).astimezone(coverage.SHANGHAI).date()
    if active["coverage_start_trade_date"] < local_today:
        blockers.append(_block("COVERAGE_START_MISSED", "新范围首日已过去且未审核启用，请重新建立接续基线；不能伪造过去日期通过"))
    if p.lease_expire_at and coverage._utc(p.lease_expire_at) > now:
        blockers.append(_block("ACCOUNT_LEASE_ACTIVE", "账户仍由执行任务持有，请等待任务结束后重新审核"))
    cursor = db.scalar(select(PortfolioReconciliationCursor).where(
        PortfolioReconciliationCursor.id == active["cursor_id"]).execution_options(populate_existing=True).with_for_update()) if current_read else db.get(PortfolioReconciliationCursor, active["cursor_id"])
    if cursor and cursor.lease_expires_at and coverage._utc(cursor.lease_expires_at) > now:
        blockers.append(_block("COVERAGE_LEASE_ACTIVE", "新覆盖队列正在处理，请等待当前核对任务结束"))
    executions = _executions(db, portfolio_id, active, current_read=current_read)
    if executions or active["verified_through_trade_date"] is not None:
        blockers.append(_block("COVERAGE_EXECUTION_ALREADY_STARTED", "本代已存在正式执行或已核验日期，应审核实际执行对账，不能改作首次范围启用", {"decision_run_ids": executions}))
    calendar = _calendar_check(db, active, current_read=current_read)
    result["checks"].append({"key": "calendar", **calendar})
    if not calendar["passed"]:
        blockers.append(_block("COVERAGE_CALENDAR_CHANGED", calendar["reason"]))
    current_data = _current_data_check(db, portfolio_id, current_read=current_read)
    current_binding = _current_binding_check(db, p, current_data, current_read=current_read)
    data_passed = all(dimensions[key] in {"READY", "RUNNING"} for key in ("status_data", "status_score")) and current_data["passed"] is True
    binding_passed = dimensions["status_model"] == "READY" and current_binding["passed"] is True and state != "MODEL_INACTIVE"
    result["checks"].extend([{"key": "data", **current_data, "passed": data_passed},
                              {"key": "binding", **current_binding, "passed": binding_passed}])
    if not data_passed:
        blockers.append(_block("CURRENT_DATA_NOT_READY", "数据或评分独立门禁未通过；" + current_data["reason"]))
    if not binding_passed:
        blockers.append(_block("CURRENT_BINDING_NOT_READY", "实际运行绑定或模型独立门禁未通过；" + current_binding["reason"]))
    result["evidence_fingerprint"] = coverage._hash({"baseline_id": active["baseline_id"],
        "generation": active["generation"], "cursor_row_version": active["row_version"],
        "baseline_evidence": active["baseline_snapshot"].get("evidence_fingerprint"),
        "source_refs": result["source_refs"], "calendar": calendar,
        "data_snapshot_id": current_data.get("snapshot_id"), "data_passed": data_passed,
        "binding_snapshot_id": current_binding.get("snapshot_id"), "binding_passed": binding_passed,
        "dimensions": dimensions, "runtime_state": state, "execution_ids": executions})
    result["can_confirm"] = not blockers
    return result


def confirm_initial_coverage_review(
    db: Session, portfolio_id: int, *, baseline_id: str, generation: int,
    expected_cursor_row_version: int, expected_evidence_fingerprint: str,
    activation_key: str, acknowledge_new_scope: bool, review_note: str, operator_id: str,
) -> dict[str, Any]:
    if not operator_id or not operator_id.strip() or operator_id.strip() in {"system", "cron_worker"}:
        raise CoverageError("PERSONAL_REVIEW_REQUIRED", "首次范围启用必须由本人审核，后台任务不能代签", status_code=403)
    if acknowledge_new_scope is not True:
        raise CoverageError("NEW_SCOPE_ACK_REQUIRED", "请明确确认只启用新范围，旧历史仍保持未知", status_code=400)
    if not review_note or len(review_note.strip()) < 10:
        raise CoverageError("REVIEW_NOTE_REQUIRED", "本人审核理由至少需要10个有效字符", status_code=400)
    if not activation_key or not activation_key.strip() or len(activation_key.strip()) > 128:
        raise CoverageError("ACTIVATION_KEY_REQUIRED", "请提供有效的首次范围审核幂等键", status_code=400)
    payload = {"baseline_id": baseline_id, "generation": generation,
        "expected_cursor_row_version": expected_cursor_row_version,
        "expected_evidence_fingerprint": expected_evidence_fingerprint, "activation_key": activation_key,
        "acknowledge_new_scope": acknowledge_new_scope, "review_note": review_note,
        "operator_id": operator_id}
    request_hash = coverage._hash(payload)
    with db.begin_nested():
        p = db.scalar(select(Portfolio).where(Portfolio.id == portfolio_id).execution_options(populate_existing=True).with_for_update())
        if p is None:
            raise CoverageError("PORTFOLIO_NOT_FOUND", "组合不存在", status_code=404)
        # Portfolio is the common serialization root for execution, baseline
        # acceptance and this audit-only activation. Never return a different
        # decision just because its caller reused an idempotency key.
        for event in _activation_events(db, portfolio_id, current_read=True):
            attrs = _json(event.attributes_json)
            if attrs.get("confirmation_type") == ACTIVATION_TYPE and attrs.get("activation_key") == activation_key:
                if attrs.get("request_hash") != request_hash:
                    raise CoverageError("ACTIVATION_KEY_REUSED", "相同启用审核幂等键已用于不同审核内容")
                return {**_json(event.after_json), "audit_event_id": event.id, "idempotent": True}
        review = _review(db, portfolio_id, current_read=True)
        if review["baseline_id"] != baseline_id or review["generation"] != generation:
            raise CoverageError("COVERAGE_GENERATION_CHANGED", "本人接受的账户基线或覆盖代次已变化，请重新审核")
        if review["cursor_row_version"] != expected_cursor_row_version:
            raise CoverageError("COVERAGE_VERSION_CHANGED", "新范围游标版本已变化，请重新预览审核")
        if review["evidence_fingerprint"] != expected_evidence_fingerprint:
            raise CoverageError("ACTIVATION_EVIDENCE_CHANGED", "账户、数据、绑定或审核状态证据已变化，请重新预览审核")
        if not review["can_confirm"]:
            codes = {item["code"] for item in review["blockers"]}
            code = "ACCOUNT_EVIDENCE_CHANGED" if "ACCOUNT_EVIDENCE_CHANGED" in codes else "INITIAL_COVERAGE_NOT_ACTIVATABLE"
            raise CoverageError(code, "当前新覆盖范围尚不能启用，请先处理审核材料中的阻断原因", details=review["blockers"])
        before = {"current_state": review["current_state"], "dimensions": review["dimensions"]}
        p.status_reconciliation = "READY"
        after_state = review["current_state"]
        if after_state == "RECONCILIATION_BLOCKED":
            # Data/binding/current independent dimensions have all been checked
            # above. ADMIN_PAUSED and every other runtime state stay in place.
            after_state = "READY"
            _set_status(db, p, after_state)
        local_today = coverage._utc(_now()).replace(tzinfo=timezone.utc).astimezone(coverage.SHANGHAI).date()
        scope_started = review["coverage_start_trade_date"] <= local_today
        result = {"portfolio_id": portfolio_id, "baseline_id": baseline_id, "generation": generation,
            "coverage_start_trade_date": review["coverage_start_trade_date"].isoformat(),
            "cursor_row_version": review["cursor_row_version"], "activation_type": ACTIVATION_TYPE,
            "current_state": after_state, "status_reconciliation": "READY",
            "scope_authorized": True, "awaiting_coverage_start": not scope_started,
            "trading_resumed": scope_started and after_state == "READY" and before["current_state"] == "RECONCILIATION_BLOCKED",
            "historical_coverage_unknown": True, "reconciliation_passed": False, "idempotent": False}
        event = write_audit_event(db, "DATA_BLOCK_RESOLUTION", portfolio_id=portfolio_id,
            business_key=baseline_id, correlation_id=activation_key, operator_id=operator_id.strip(),
            before=before, after=result, attributes={"confirmation_type": ACTIVATION_TYPE,
                "baseline_id": baseline_id, "generation": generation,
                "new_scope_start": review["coverage_start_trade_date"].isoformat(),
                "history_gaps": review.get("history_gaps", []), "historical_coverage_unknown": True,
                "review_note": review_note.strip(), "ack": True, "source_verified": True,
                "source_refs": review["source_refs"], "evidence_fingerprint": review["evidence_fingerprint"],
                "activation_key": activation_key, "request_hash": request_hash,
                "reconciliation_passed": False, "trading_resumed": result["trading_resumed"]},
            note="本人核验并启用新范围的首次执行；旧历史保持未知，未将任何交易日记为对账通过")
        db.flush()
        return {**result, "audit_event_id": event.event_id}


def assert_execution_within_active_coverage(db: Session, portfolio_id: int, trade_date: date) -> None:
    """Keep legacy execution compatible; enforce real accepted scopes when set."""
    with db.no_autoflush:
        db.scalar(select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update())
        active = _active(db, portfolio_id, current_read=True)
        if active is None:
            return
        if trade_date < active["coverage_start_trade_date"]:
            raise CoverageError("OUTSIDE_CURRENT_COVERAGE", "正式执行日期早于本人接受的新覆盖起点，旧历史不能由新基线补跑")
        if get_initial_coverage_activation(db, portfolio_id, active=active, current_read=True) is None:
            raise CoverageError("INITIAL_COVERAGE_REVIEW_REQUIRED", "当前基线和代次尚未完成本人首次范围启用审核")
