"""Read-only runtime permissions and recovery prerequisites for inspection.

The operation matrix is combined with every persisted blocking dimension.
This does not accept bindings, clear a lock, execute tasks, or create baselines.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio, PORTFOLIO_STATUS_VALUES
from app.services.portfolio_reconciliation import latest_reconciliation_results
from app.services.portfolio_state_machine import VALID_STATES, _check_allows_action, _get_status


def _runtime_now() -> datetime:
    return datetime.now(timezone.utc)


def _current_data_check(db: Session, portfolio_id: int, *, current_read: bool = False) -> dict[str, Any]:
    """Use execution's business-date evidence, including verified scope reads.

    Holding-only gaps stay visible without silently stopping every other symbol.
    An unknown source that affects admission cannot count as a passed check.
    """
    from app.services.portfolio_inspection_diagnostics import build_portfolio_inspection
    try:
        import inspect as python_inspect
        supports_current_read = "current_read" in python_inspect.signature(build_portfolio_inspection).parameters
        if current_read and supports_current_read:
            inspection = build_portfolio_inspection(db, portfolio_id, current_read=True)
        elif current_read and db.get_bind().dialect.name not in {"sqlite"}:
            raise ValueError("current_data_read_unavailable")
        else:
            inspection = build_portfolio_inspection(db, portfolio_id)
    except Exception:
        return {"passed": None, "reason": "当前数据检查读取失败，请重新读取后再审核恢复",
                "snapshot_id": None, "evidence": [], "reason_codes": ["DATA_INSPECTION_READ_FAILED"],
                "has_admission_scope": None, "binding_evidence": []}
    return evaluate_inspection_admission_scope(inspection)


def evaluate_inspection_admission_scope(inspection: dict[str, Any]) -> dict[str, Any]:
    """Evaluate already-read evidence with the execution aggregation policy.

    Confirmed repairs use their locked final inspection here, so proving the
    data dimensions complete cannot use a different rule from runtime review.
    This pure function grants no permissions and changes no persisted state.
    """
    from app.services.portfolio_inspection_diagnostics import inspection_admission_failures
    if inspection.get("status") == "READ_ERROR":
        return {"passed": None, "reason": inspection.get("reason") or "当前数据检查读取失败",
                "snapshot_id": inspection.get("snapshot_id"), "evidence": [],
                "reason_codes": ["DATA_INSPECTION_READ_FAILED"], "has_admission_scope": None,
                "aggregation_policy": "existing_any_healthy_symbol_v1", "restricted_symbol_ids": [],
                "binding_evidence": []}
    global_evidence = inspection.get("evidence", [])
    global_data = [ev for ev in global_evidence if ev.get("check_code") not in {
        "STRATEGY_BINDING", "RULE_BINDING", "EXPECTED_TRADE_DATE"}]
    global_failures = inspection_admission_failures(global_data)
    applicable = list(global_failures)
    healthy_ids, restricted_ids = [], []
    for row in inspection.get("symbols", []):
        failures = [ev for ev in inspection_admission_failures(row.get("evidence", []))
            if set(ev.get("affected_capabilities", [])) & {
                "new_buy", "automatic_recovery"}]
        applicable.extend(failures)
        if row.get("entry_authorized"):
            (restricted_ids if failures else healthy_ids).append(row.get("symbol_id"))
    unknown = {"READ_ERROR", "CALENDAR_UNKNOWN", "UNKNOWN"}
    # Preserve the execution readiness policy: one healthy entry symbol is
    # sufficient at portfolio level. Every bad symbol is still rejected by
    # the same per-symbol check at order entry. Scope read errors affect all.
    portfolio_failures = global_failures or ([] if healthy_ids else applicable)
    passed = (None if any(ev.get("status") in unknown for ev in portfolio_failures) else False) if portfolio_failures else True
    reason = (f"现行聚合规则至少一个标的可用：{len(healthy_ids)} 个正常，{len(restricted_ids)} 个仅限制该标的新买入" if passed and restricted_ids else
              "参与新买入与自动恢复范围的当前业务日数据已通过检查" if passed else
              "；".join(dict.fromkeys(str(ev.get("reason") or "数据证据尚未核实") for ev in applicable)))
    return {"passed": passed, "reason": reason, "snapshot_id": inspection.get("snapshot_id"),
            "evidence": applicable, "reason_codes": sorted({ev.get("check_code", "DATA_UNKNOWN") for ev in applicable}),
            "has_admission_scope": any(row.get("entry_authorized") for row in inspection.get("symbols", [])),
            "aggregation_policy": "existing_any_healthy_symbol_v1", "restricted_symbol_ids": restricted_ids,
            "coverage": {"entry_symbols": len(healthy_ids) + len(restricted_ids),
                         "healthy_symbols": len(healthy_ids), "restricted_symbols": len(restricted_ids)},
            "binding_evidence": [ev for ev in inspection_admission_failures(global_evidence) if ev.get("check_code") in {
                "STRATEGY_BINDING", "RULE_BINDING"}]}


def _current_binding_check(db: Session, portfolio: Portfolio, current_data: dict, *, current_read: bool = False) -> dict:
    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.services.portfolio_binding_review import build_binding_review
    statement = select(StrategyExecutionSnapshot.id).where(
        StrategyExecutionSnapshot.portfolio_id == portfolio.id,
        StrategyExecutionSnapshot.snapshot_type == "save_and_apply").order_by(StrategyExecutionSnapshot.id).limit(1)
    applied = db.scalar(statement.with_for_update() if current_read else statement)
    if not applied and current_data["has_admission_scope"] is False and not current_data["binding_evidence"]:
        return {"passed": True, "not_applicable": True,
                "reason": "当前未启用自动执行范围，也没有已应用运行绑定，本次无需确认绑定"}
    try:
        review = build_binding_review(db, portfolio, current_read=current_read)
    except Exception:
        return {"passed": None, "reason": "运行绑定读取失败，请重新读取后审核"}
    reasons = list(review["blockers"]) + [str(ev.get("reason")) for ev in current_data["binding_evidence"]]
    if review["requires_confirmation"]:
        reasons.append(review["change_summary"])
    return {"passed": not reasons, "reason": "当前实际运行绑定与已确认范围一致" if not reasons else "；".join(dict.fromkeys(reasons)),
            "snapshot_id": review.get("snapshot_id"), "requires_confirmation": review["requires_confirmation"]}


def get_portfolio_runtime_inspection(db: Session, portfolio_id: int, *, current_read: bool = False) -> dict[str, Any]:
    with db.no_autoflush:
        return _read_runtime(db, portfolio_id, current_read=current_read)


def _read_runtime(db: Session, portfolio_id: int, *, current_read: bool = False) -> dict[str, Any]:
    p = db.get(Portfolio, portfolio_id, with_for_update=current_read, populate_existing=current_read)
    if p is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    state = _get_status(db, p, current_read=current_read)
    dimensions = {key: getattr(getattr(p, key), "value", getattr(p, key)) for key in (
        "status_score", "status_data", "status_model", "status_reconciliation")}
    invalid = state not in VALID_STATES or any(value not in PORTFOLIO_STATUS_VALUES for value in dimensions.values())
    reasons = []
    labels = {"status_score": "评分检查", "status_data": "数据完整性",
              "status_model": "运行绑定", "status_reconciliation": "对账审核"}
    for key, value in dimensions.items():
        if value not in {"READY", "RUNNING"}:
            reasons.append(f"{labels[key]}尚未通过（{value or '未知'}）")
    state_reasons = {
        "PENDING_INITIAL_REVIEW": "首次启用尚待本人审核",
        "RECONCILIATION_BLOCKED": "对账异常暂停尚待证据重检和本人确认",
        "ADMIN_PAUSED": "本人暂停尚未解除，需要独立审核备注",
        "DATA_INCOMPLETE_PAUSED": "运行状态仍有数据缺口",
        "SCORE_STALE": "运行状态仍有评分过期",
        "MODEL_INACTIVE": "当前运行绑定尚未确认",
        "INTERRUPTED": "运行中断，需核实任务恢复条件",
    }
    if state in state_reasons:
        reasons.insert(0, state_reasons[state])
    if invalid:
        reasons.insert(0, "状态记录无效或缺失，当前能力无法判定")
    current_data = _current_data_check(db, portfolio_id, current_read=current_read)
    current_binding = _current_binding_check(db, p, current_data, current_read=current_read)
    fresh_data_passed = current_data["passed"]
    ready_dimensions = not any(value not in {"READY", "RUNNING"} for value in dimensions.values())
    allow_buys = None if invalid else _check_allows_action(state, "NEW_BUY") and ready_dimensions and state != "MODEL_INACTIVE" and fresh_data_passed is True and current_binding["passed"] is True
    allow_exits = None if invalid else _check_allows_action(state, "RISK_SELL")
    review_required = state in {"PENDING_INITIAL_REVIEW", "RECONCILIATION_BLOCKED", "ADMIN_PAUSED", "MODEL_INACTIVE"} or any(
        dimensions[key] != "READY" for key in ("status_model", "status_reconciliation"))
    auto_recovery = None if invalid else _check_allows_action(state, "AUTO_RECOVERY_FLOW") and ready_dimensions and not review_required and fresh_data_passed is True and current_binding["passed"] is True
    if fresh_data_passed is not True:
        reasons.append(current_data["reason"])
    if current_binding["passed"] is not True:
        reasons.append(current_binding["reason"])

    report_available = "data_governance_audit_events" in inspect(db.connection()).get_table_names()
    results = latest_reconciliation_results(db, portfolio_id, current_read=current_read) if report_available else {}
    from app.services.portfolio_reconciliation import _active_coverage
    coverage_read_error = None
    try:
        active = _active_coverage(db, portfolio_id, current_read=current_read)
    except ValueError as exc:
        active = None
        coverage_read_error = str(exc)
        allow_buys = False if not invalid else None
        auto_recovery = False if not invalid else None
        review_required = True
        reasons.append("账户基线或当前覆盖来源不可核验：" + coverage_read_error)
    activation_review = None
    activation = None
    before_coverage_start = False
    if active:
        from app.services.portfolio_reconciliation_activation import get_initial_coverage_activation, get_coverage_activation_review
        activation = get_initial_coverage_activation(db, portfolio_id, active=active, current_read=current_read)
        if not activation:
            activation_review = get_coverage_activation_review(db, portfolio_id)
            allow_buys = False if not invalid else None
            auto_recovery = False if not invalid else None
            review_required = True
            reasons.append("当前新覆盖范围尚未经过首日执行前的本人审核")
        if _runtime_now().astimezone(ZoneInfo("Asia/Shanghai")).date() < active["coverage_start_trade_date"]:
            before_coverage_start = True
            allow_buys = False if not invalid else None
            auto_recovery = False if not invalid else None
            reasons.append(f"覆盖起点为 {active['coverage_start_trade_date'].isoformat()}；当前尚未到新起点，不能新买入或自动恢复")
        results = {day: attrs for day, attrs in results.items() if
            day >= active["coverage_start_trade_date"] and not attrs.get("outside_current_coverage") and
            attrs.get("coverage_generation", attrs.get("generation")) == active["generation"] and
            attrs.get("baseline_id") == active["baseline_id"]}
        target = p.last_decision_trade_date if p.last_decision_trade_date and p.last_decision_trade_date >= active["coverage_start_trade_date"] else active["verified_through_trade_date"] or active["coverage_start_trade_date"]
    else:
        target = p.last_decision_trade_date or max(results, default=None)
        # Old audit entries may include checks of a later day. Keep the guide
        # on the same earliest unresolved day as the retained queue/checkpoint.
        from app.services.portfolio_resume_service import get_reconciliation_queue
        queue = get_reconciliation_queue(db, portfolio_id, today=_runtime_now().astimezone(ZoneInfo("Asia/Shanghai")).date())
        if queue.get("calendar_status") == "KNOWN" and queue.get("target_trade_date"):
            target = date.fromisoformat(queue["target_trade_date"])
    latest = results.get(target, {}) if target else {}
    unresolved = [day for day, attrs in results.items() if attrs.get("status") == "BLOCKED"
                  and attrs.get("work_state") != "waiting_execution"]
    # A never-executed portfolio has no execution day to reconcile. This is an
    # explicit non-applicable check, not a manufactured historical pass.
    from app.models.decision_engine import DecisionRun
    execution_query = db.query(DecisionRun.id).filter(DecisionRun.portfolio_id == portfolio_id,
        DecisionRun.run_type == "auto_simulation")
    if active:
        execution_query = execution_query.filter(DecisionRun.trade_date >= active["coverage_start_trade_date"])
    has_execution = (execution_query.with_for_update() if current_read else execution_query).first() is not None
    first_scope_before_execution = bool(active and active["verified_through_trade_date"] is None and not has_execution
        and not any(attrs.get("verdict", attrs.get("status")) == "DIFF_FOUND" for attrs in results.values()))
    reconciliation_required = bool(target or results or has_execution or
        state == "RECONCILIATION_BLOCKED" or dimensions["status_reconciliation"] == "RECONCILIATION_BLOCKED")
    if first_scope_before_execution:
        reconciliation_required = False
    reconciliation_passed = not reconciliation_required or bool(latest.get("status") == "PASSED" and not unresolved)
    from app.services.portfolio_reconciliation_events import pending_source_events
    from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationDay, PortfolioReconciliationCursor
    source_events = pending_source_events(db, portfolio_id, current_read=current_read)
    pending_days = []
    if active:
        day_statement = select(PortfolioReconciliationDay).where(
            PortfolioReconciliationDay.cursor_id == active["cursor_id"], PortfolioReconciliationDay.generation == active["generation"],
            PortfolioReconciliationDay.requested_evidence_version > PortfolioReconciliationDay.processed_evidence_version
        ).order_by(PortfolioReconciliationDay.target_trade_date).execution_options(populate_existing=True)
        pending_days = list(db.scalars(day_statement.with_for_update() if current_read else day_statement))
    changed_source = bool(active and (source_events["count"] or source_events["status"] == "MIGRATION_REQUIRED" or
        any(row.account_source_pending or row.work_state not in {"waiting_execution"} for row in pending_days)))
    if changed_source:
        reconciliation_passed = False
        allow_buys = False if not invalid else None
        auto_recovery = False if not invalid else None
        reasons.append("来源或核对需求已有变化，旧报告需重新核对；不能沿用旧通过结果")
    if coverage_read_error:
        reconciliation_passed = False
    waiting = latest.get("work_state") == "waiting_execution"
    dimension_data_passed = all(dimensions[key] in {"READY", "RUNNING"} for key in ("status_data", "status_score"))
    data_passed = dimension_data_passed and fresh_data_passed is True
    binding_passed = dimensions["status_model"] == "READY" and current_binding["passed"] is True and state != "MODEL_INACTIVE"
    review_states = {"PENDING_INITIAL_REVIEW", "ADMIN_PAUSED", "INTERRUPTED", "RECONCILIATION_BLOCKED",
                     "DATA_INCOMPLETE_PAUSED", "SCORE_STALE", "MODEL_INACTIVE"}
    runtime_reviewable = state in review_states or activation_review is not None
    checks = [
        {"key": "reconciliation", "passed": False if changed_source else True if not reconciliation_required else None if not report_available or not latest or waiting else reconciliation_passed,
         "reason": "账户来源或核对需求已变化，需先消费来源事件并重检；原报告及审核记录保留" if changed_source else "新范围尚无已发生执行日，首日准入需本人单独审核；旧期历史未知保留" if first_scope_before_execution else "尚无自动执行日期，本次首次启用不适用执行对账" if not reconciliation_required else "等待合法执行结果，尚不需要本人确认" if waiting else
                   "当前日期报告已通过，其他已知日期无未解决异常" if reconciliation_passed else
                   "缺少当前日期的有效通过报告，或仍有其他日期未完成",
         "not_applicable": not reconciliation_required and not changed_source,
         "trade_date": target.isoformat() if target else None,
         "report_id": latest.get("report_id"), "report_version": latest.get("report_version")},
        {"key": "data", "passed": None if invalid or (dimension_data_passed and fresh_data_passed is None) else data_passed,
         "not_applicable": data_passed and current_data.get("has_admission_scope") is False,
         "reason": "当前无授权新买入标的，本步不适用；持仓和候选数据问题仍可查看" if data_passed and current_data.get("has_admission_scope") is False else
                   current_data["reason"] if dimension_data_passed else "数据/评分仍有独立阻断；" + current_data["reason"],
         "snapshot_id": current_data["snapshot_id"], "reason_codes": current_data["reason_codes"],
         "evidence": current_data["evidence"], "coverage": current_data.get("coverage"),
         "restricted_symbol_ids": current_data.get("restricted_symbol_ids", []),
         "aggregation_policy": current_data.get("aggregation_policy")},
        {"key": "binding", "passed": None if invalid else binding_passed,
         "reason": current_binding["reason"] if dimensions["status_model"] == "READY" else "当前绑定仍需本人核实与确认；" + current_binding["reason"],
         "snapshot_id": current_binding.get("snapshot_id"), "not_applicable": current_binding.get("not_applicable", False)},
        {"key": "runtime", "passed": None if invalid else runtime_reviewable or state in {"READY", "RUNNING"},
         "reason": "当前状态允许在核实全部材料后提交本人审核" if runtime_reviewable else "当前状态无需重复提交恢复审核"},
    ]
    blocked_checks = [c["reason"] for c in checks if c["passed"] is not True]
    can_confirm = (not invalid and runtime_reviewable and
                   reconciliation_passed and data_passed and binding_passed and
                   latest.get("progression", {}).get("can_advance", True))
    if not latest.get("progression", {}).get("can_advance", True):
        blocked_checks.append("连续核验范围尚未接续，不能跳过前序日期")
    if coverage_read_error:
        can_confirm = False
        blocked_checks.append("账户基线或当前覆盖来源不可核验：" + coverage_read_error)
    confirmation_action = {"RECONCILIATION_BLOCKED": "RECONCILIATION_CONFIRM",
        "PENDING_INITIAL_REVIEW": "INITIAL_REVIEW", "ADMIN_PAUSED": "RESUME_ADMIN_PAUSE",
        "INTERRUPTED": "REVIEW_INTERRUPTED"}.get(state, "REVIEW_RUNTIME")
    if activation_review is not None:
        confirmation_action = "INITIAL_COVERAGE_REVIEW"
        can_confirm = not invalid and activation_review.get("can_confirm", False)
        blocked_checks = [item.get("message", item.get("code", "新范围审核材料未通过")) if isinstance(item, dict) else str(item)
                          for item in activation_review.get("blockers", [])]
        checks.append({"key": "coverage", "passed": can_confirm,
                       "reason": "已接受当前账户基线，首日执行前请审核新的覆盖范围" if can_confirm else "新覆盖范围的来源或义务尚未重新核实"})
    if changed_source:
        can_confirm = False
        blocked_checks.append("来源事件或日期工作单待重检，当前材料尚不能确认恢复")
    # Waiting is an operational phase, not an invitation to sign another
    # recovery acknowledgement. Preserve legacy action codes while returning
    # a precise recommendation for clients that render the next step.
    next_check_at = latest.get("next_check_at")
    waiting_reason = None
    if before_coverage_start and activation:
        phase, recommended_action = "WAITING_COVERAGE_START", "WAIT_COVERAGE_START"
        waiting_reason = f"当前范围已审核，等待覆盖起点 {active['coverage_start_trade_date'].isoformat()}，无需重复审核"
        next_check_at = datetime.combine(active["coverage_start_trade_date"], datetime.min.time(), ZoneInfo("Asia/Shanghai")).isoformat()
        can_confirm = False
    elif waiting:
        phase, recommended_action = "WAITING_EXECUTION", "WAIT_EXECUTION"
        waiting_reason = "等待合法执行或撮合结果到齐后自动核对；当前无需确认恢复"
        can_confirm = False
    elif can_confirm:
        phase, recommended_action = "REVIEW_REQUIRED", confirmation_action
    elif blocked_checks:
        phase, recommended_action = "BLOCKED", "VIEW_BLOCKERS"
    else:
        phase, recommended_action = "NO_REVIEW_REQUIRED", "NONE"
    guide = _guide_proof(db, portfolio_id, checks, active, latest, pending_days, source_events,
        current_read=current_read,
        reports=[{"trade_date": day.isoformat(), **{key: attrs.get(key) for key in (
            "report_id", "report_version", "persistent_report_id", "persistent_report_version", "status", "work_state")}}
            for day, attrs in sorted(results.items())],
        changed_source=changed_source, can_confirm=can_confirm, state=state, invalid=invalid,
        waiting=waiting, activation_review=activation_review, permissions={"allow_new_buys": allow_buys,
            "allow_risk_exits": allow_exits, "allow_auto_recovery": auto_recovery})
    if can_confirm and not guide["can_confirm"]:
        can_confirm = False
        phase, recommended_action = "BLOCKED", "VIEW_BLOCKERS"
        blocked_checks.append("前置步骤仍在处理或证据未知，请等待结果并重检后再审核")
    return {"portfolio_id": portfolio_id, "generated_at": _runtime_now().isoformat(), "guide": guide,
        "source_events": source_events,
        "current_state": state, "dimensions": dimensions,
        "permissions": {"allow_new_buys": allow_buys, "allow_risk_exits": allow_exits,
                        "allow_auto_recovery": auto_recovery, "requires_manual_ack": review_required or invalid},
        "capability_reasons": {
            "allow_new_buys": reasons if not allow_buys else ["当前运行状态与四个独立维度均允许新买入"],
            "allow_risk_exits": ["状态未知，需重新读取退出能力"] if allow_exits is None else
                                ["当前状态门禁允许风险退出；成交仍需持仓与订单条件满足"] if allow_exits else
                                ["当前状态门禁禁止风险退出，请核实退出限制"],
            "allow_auto_recovery": reasons or (["异常恢复仍需本人审核"] if review_required else
                                                ["当前运行状态允许自动恢复流程"])},
        "recovery_summary": {"can_confirm": can_confirm, "blockers": blocked_checks,
                             "checks": checks, "review_required": review_required,
                             "confirmation_action": confirmation_action,
                             "phase": phase, "recommended_action": recommended_action,
                             "waiting_reason": waiting_reason, "next_check_at": next_check_at,
                             "coverage_activation_review": activation_review,
                             "target_state": "READY",
                             "force_recovery_enabled": False},
        "coverage": {key: value for key, value in active.items() if key != "baseline_snapshot"} if active else None,
        "limitations": (["HISTORICAL_EVIDENCE_REMAINS_UNKNOWN"] if active and active.get("history_gaps") else []) if active else ["NO_ACCEPTED_ACCOUNT_BASELINE", "LEGACY_REPORT_AUDIT_ADAPTER"]}


def _guide_proof(db, portfolio_id, checks, active, latest, pending_days, source_events, *,
        current_read, reports, changed_source, can_confirm, state, invalid, waiting, activation_review, permissions):
    """Evidence-derived states; visiting a step never creates a completion fact."""
    from app.services.portfolio_reconciliation_coverage import _hash
    from app.models.async_task import AsyncTaskRecord
    import json
    tasks = []
    if "async_tasks" in inspect(db.connection()).get_table_names():
        task_statement = select(AsyncTaskRecord).where(AsyncTaskRecord.task_type.in_([
            "portfolio_data_repair", "portfolio_auto_simulation", "portfolio_resume"]),
            AsyncTaskRecord.status.in_(["queued", "running"])).execution_options(populate_existing=True)
        for task in db.scalars(task_statement.with_for_update() if current_read else task_statement):
            try:
                payload = json.loads(task.payload_json or "{}")
            except ValueError:
                continue
            if payload.get("portfolio_id") == portfolio_id:
                tasks.append({"task_id": task.id, "task_type": task.task_type, "status": task.status})
    steps = []
    labels = ["核实执行", "批量修复数据", "确认运行绑定", "本人审核恢复"]
    for index, key in enumerate(["reconciliation", "data", "binding", "runtime"]):
        check = next(row for row in checks if row["key"] == key)
        proof = {"check": check, "scope": {k: active.get(k) for k in ("baseline_id", "generation")} if active else None}
        if key == "reconciliation":
            proof.update(report_id=latest.get("report_id"), report_version=latest.get("report_version"),
                reports=reports, verified_through_trade_date=active.get("verified_through_trade_date") if active else None,
                pending_days=[{"trade_date": row.target_trade_date, "requested": row.requested_evidence_version,
                    "processed": row.processed_evidence_version, "state": row.work_state} for row in pending_days],
                source_event_ids=[row["id"] for row in source_events["events"]], source_event_fingerprint=source_events.get("fingerprint"))
        running = [row for row in tasks if (key == "data") == (row["task_type"] == "portfolio_data_repair") and key in {"data", "reconciliation"}]
        if key == "runtime":
            status = "UNAVAILABLE" if invalid else "PASSED" if permissions["allow_new_buys"] else "INCOMPLETE"
            proof.update(permissions=permissions, can_confirm=can_confirm, activation=activation_review)
        elif key == "reconciliation" and changed_source:
            status = "NEEDS_RECHECK"
        elif running or key == "reconciliation" and any(row.work_state == "running" for row in pending_days):
            status = "RUNNING"
        elif check.get("not_applicable"):
            status = "NOT_APPLICABLE"
        elif check["passed"] is True:
            status = "PASSED"
        elif check["passed"] is None:
            status = "WAITING" if key == "reconciliation" and waiting else "UNAVAILABLE"
        else:
            status = "INCOMPLETE"
        steps.append({"key": key, "label": labels[index], "status": status, "reason": check["reason"],
            "proof_hash": _hash(proof), "proof": proof, "tasks": running,
            "next_action": "VIEW_JOB" if status in {"RUNNING", "WAITING"} else "RECHECK_SOURCES" if status == "NEEDS_RECHECK" else
                "REVIEW_CURRENT_SCOPE" if key == "runtime" else "OPEN_STEP"})
    can_confirm = can_confirm and all(row["status"] in {"PASSED", "NOT_APPLICABLE"} for row in steps[:3])
    return {"schema_version": "inspection-guide-v1", "steps": steps,
        "snapshot_id": _hash({"portfolio_id": portfolio_id, "steps": steps}), "can_confirm": can_confirm,
        "completion_proofs": [{"key": row["key"], "status": row["status"], "proof_hash": row["proof_hash"],
            "reason": row["reason"], "proof": row["proof"]} for row in steps[:3]],
        "effects": {"target_state": "READY", "current_permissions": permissions,
            "historical_unknown_retained": bool(active and active.get("history_gaps")), "force_recovery_enabled": False}}
