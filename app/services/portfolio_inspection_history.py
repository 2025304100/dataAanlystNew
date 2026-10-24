"""Query immutable, real governance audit records as inspection batches.

The legacy store has JSON snapshots rather than an object-index table. This
adapter indexes only explicit structured fields. It deliberately does not
parse security codes, dates, approvals, or successful execution from prose.
All filtering and grouping precede pagination; no database writes occur.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.symbol import Symbol
from app.schemas.portfolio_inspection_history import (
    ExecutionResult, HistoryBatchDetail, HistoryEvent, HistoryFilters,
    HistoryObject, HistoryPage, HistoryReference, HistorySecurity,
    HistorySecurityPage, ReviewDecision,
)
from app.services.data_governance_audit import DataGovernanceAuditEvent


DISPLAY_TZ = timezone(timedelta(hours=8))
ACTION_LABELS = {
    "PORTFOLIO_CANDIDATE_SCD2_CHANGE": "候选范围变更",
    "BENCHMARK_SOURCE_FAILOVER": "基准来源切换",
    "AUTO_SIMULATION_RESULT": "自动运行结果",
    "RECONCILIATION_RESULT": "每日核对结果",
    "ILLEGAL_STATE_TRANSITION": "状态转移被拒绝",
    "FACTOR_USAGE_APPLIED": "模型与策略变更",
    "OUTBOX_EVENT_DISPATCHED": "任务分发结果",
    "DATA_BLOCK_RESOLUTION": "数据阻断处理",
    "DATA_SOURCE_FAILOVER": "数据来源切换",
    "DATA_QUALITY_QUARANTINE": "数据隔离处置",
    "G6_ROLLOUT_STARTED": "运行范围扩大",
    "G6_ROLLOUT_ROLLED_BACK": "运行范围回退",
    "MARKET_BAR_CORRECTION": "行情证据纠错",
    "UNKNOWN_AUDIT_ACTION": "未登记操作",
    "REPAIR_TASK_REQUESTED": "提交数据修复任务",
    "REPAIR_OBJECT_RESULT": "逐对象修复结果",
    "REPAIR_TASK_RESULT": "数据修复任务结果",
    "ACCOUNT_BASELINE_ACCEPTANCE_REJECTED": "账户新覆盖起点未被接受",
    "INSPECTION_AI_ANALYSIS": "AI 辅助分析检查证据",
    "RECONCILIATION_SOURCE_RECEIVED": "来源变动已登记待重检",
    "INSPECTION_MATERIAL_INVESTIGATED": "登记历史材料核查结果（未解除阻断）",
}
REVIEW_MAP: dict[str, ReviewDecision] = {
    "pending": "pending", "pending_review": "pending", "approved": "approved",
    "accepted": "approved", "agree": "approved", "rejected": "rejected",
    "denied": "rejected", "revoked": "revoked", "withdrawn": "revoked",
    "unknown": "unknown", "not_applicable": "not_applicable",
}
EXECUTION_MAP: dict[str, ExecutionResult] = {
    "not_executed": "not_executed", "not_started": "not_executed",
    "pending": "not_executed", "queued": "not_executed", "running": "running",
    "in_progress": "running", "success": "success", "succeeded": "success",
    "passed": "success", "completed": "success", "partial_failure": "partial_failure",
    "partial_failed": "partial_failure", "partially_succeeded": "partial_failure",
    "done": "success", "partial": "partial_failure", "dependency_failed": "dependency_blocked",
    "failed": "failed", "failure": "failed", "interrupted": "failed",
    "dependency_blocked": "dependency_blocked", "blocked": "dependency_blocked",
    "unknown": "unknown", "not_checked": "unknown", "not_applicable": "not_applicable",
    "still_blocked": "dependency_blocked",
}
OBJECT_KEYS = ("objects", "object_results", "affected_objects", "inspection_objects")
# These identify one treatment. An issue/report alone may have several batches.
GROUP_KEYS = ("batch_id", "handling_batch_id", "review_batch_id", "operation_id", "plan_id", "task_id")
REFERENCE_KEYS = {
    "issue_id": "issue", "issue_ids": "issue", "correlation_id": "correlation",
    "report_ref": "report", "report_id": "report", "report_refs": "report",
    "report_reference": "report", "plan_id": "plan", "task_id": "task",
    "task_ids": "task", "decision_run_id": "run", "run_id": "run",
    "batch_id": "batch", "handling_batch_id": "batch", "review_batch_id": "batch",
    "operation_id": "operation", "snapshot_id": "snapshot", "evidence_ref": "evidence",
    "baseline_id": "baseline",
    "evidence_refs": "evidence", "source_record_ref": "source_record",
    "task_ref": "task",
    "order_id": "order", "order_ids": "order", "trade_id": "trade", "trade_ids": "trade",
    "ledger_id": "ledger", "cash_ledger_id": "ledger", "ledger_ids": "ledger",
    "g5_report_id": "report", "audit_report_id": "report",
    "evidence_id": "evidence", "decision_evidence_id": "evidence",
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return str(value).strip() if isinstance(value, (str, int)) and str(value).strip() else None


def _decoded(raw: str | None, label: str, warnings: list[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        warnings.append(f"{label}格式损坏，保留原始内容，相关历史字段未知")
        return raw


def _at(value: datetime) -> datetime:
    # Existing database timestamps follow the UTC-naive storage contract.
    # Retain the exact source value separately for legacy investigations.
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(DISPLAY_TZ)


def _date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def normalized_filters(filters: HistoryFilters, *, now: datetime | None = None) -> HistoryFilters:
    result = filters.model_copy(deep=True)
    now = _at(now or datetime.now(DISPLAY_TZ))
    if result.occurred_from is None and result.occurred_to is None:
        result.occurred_from, result.occurred_to = now - timedelta(days=30), now
    if result.occurred_from:
        result.occurred_from = result.occurred_from.replace(tzinfo=DISPLAY_TZ) if result.occurred_from.tzinfo is None else result.occurred_from.astimezone(DISPLAY_TZ)
    if result.occurred_to:
        result.occurred_to = result.occurred_to.replace(tzinfo=DISPLAY_TZ) if result.occurred_to.tzinfo is None else result.occurred_to.astimezone(DISPLAY_TZ)
    if result.occurred_from and result.occurred_to and result.occurred_from > result.occurred_to:
        raise ValueError("发生时间起点不能晚于终点")
    if result.trade_date_from and result.trade_date_to and result.trade_date_from > result.trade_date_to:
        raise ValueError("所属交易日起点不能晚于终点")
    if result.report_version and not result.report_ref:
        raise ValueError("报告版本筛选必须同时提供报告引用")
    result.security_ids = sorted(set(item.strip() for item in result.security_ids if item.strip()))
    result.actions = sorted(set(item.strip() for item in result.actions if item.strip()))
    if len(result.security_ids) > 100:
        raise ValueError("单次最多选择100个标的")
    if any(not re.match(r"^[A-Za-z][A-Za-z0-9_-]*[:.][^\s]+$", item) for item in result.security_ids):
        raise ValueError("标的筛选必须使用含市场身份的security_id")
    return result


def _references(value: Any, event_id: int) -> list[HistoryReference]:
    found: dict[tuple[str, str, str | None], HistoryReference] = {}

    def add(kind: str, raw: Any, parent: dict[str, Any]) -> None:
        if isinstance(raw, list):
            for child in raw:
                add(kind, child, parent)
            return
        detail = raw if isinstance(raw, dict) else parent
        ref = _text(raw) if not isinstance(raw, dict) else _text(raw.get("ref") or raw.get("id") or raw.get("report_ref") or raw.get("report_id"))
        if not ref:
            return
        version = _text(detail.get("version") or detail.get("report_version")) if kind == "report" else None
        status = _text(detail.get("retention_status") or detail.get("material_status")) or "unknown"
        found[(kind, ref, version)] = HistoryReference(
            kind=kind, ref=ref, version=version, retention_status=status,
            source_event_id=event_id,
        )

    def visit(raw: Any) -> None:
        if isinstance(raw, list):
            for child in raw:
                visit(child)
        elif isinstance(raw, dict):
            # Explicit generic reference objects, including report versions.
            if raw.get("kind") and (raw.get("ref") or raw.get("id")):
                add(str(raw["kind"]), raw, raw)
            for key, child in raw.items():
                if key in REFERENCE_KEYS:
                    add(REFERENCE_KEYS[key], child, raw)
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(value)
    return list(found.values())


def _unique_refs(refs: list[HistoryReference]) -> list[HistoryReference]:
    # Keep versions and sources: a later reference cannot rewrite an old report.
    return list({(r.kind, r.ref, r.version, r.source_event_id): r for r in refs}.values())


def _review(event: DataGovernanceAuditEvent, attrs: dict[str, Any]) -> ReviewDecision:
    raw = attrs.get("review_decision") or attrs.get("approval_decision")
    if raw is None and isinstance(attrs.get("review"), dict):
        raw = attrs["review"].get("decision")
    if raw is not None:
        return REVIEW_MAP.get(str(raw).lower(), "unknown")
    if attrs.get("confirmation_type") in {"MODEL_BINDING_CONFIRMED", "RECONCILIATION_CONFIRMED", "INITIAL_COVERAGE_REVIEW_CONFIRMED"} and attrs.get("ack") is True:
        return "approved"
    return "unknown"


def _execution(event: DataGovernanceAuditEvent, attrs: dict[str, Any]) -> ExecutionResult:
    raw = attrs.get("execution_result") or attrs.get("execution_status")
    if isinstance(raw, dict):
        raw = raw.get("status")
    if raw is None:
        raw = attrs.get("result_status")
    # Legacy status is meaningful only for a documented result event. A
    # confirmation or a READY state is not proof of execution success.
    if raw is None and event.action in {"AUTO_SIMULATION_RESULT", "RECONCILIATION_RESULT", "OUTBOX_EVENT_DISPATCHED"}:
        raw = attrs.get("status")
    if raw is None and attrs.get("confirmation_type") == "INITIAL_COVERAGE_REVIEW_CONFIRMED":
        return "not_executed"
    return EXECUTION_MAP.get(str(raw).lower(), "unknown") if raw is not None else "unknown"


def _catalogue(db: Session) -> dict[int, dict[str, Any]]:
    return {
        row.id: {"symbol_id": row.id, "code": row.symbol, "name": row.name, "market": row.market,
                 "security_id": f"{row.market.upper()}:{row.symbol}"}
        for row in db.execute(select(Symbol.id, Symbol.symbol, Symbol.name, Symbol.market))
        if row.market and row.symbol
    }


def _objects(event: DataGovernanceAuditEvent, attrs: dict[str, Any], catalogue: dict[int, dict[str, Any]]) -> tuple[list[HistoryObject], list[str]]:
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    for key in OBJECT_KEYS:
        if isinstance(attrs.get(key), list):
            candidates.extend(item for item in attrs[key] if isinstance(item, dict))
    if not candidates and isinstance(attrs.get("diffs"), list):
        candidates = [item for item in attrs["diffs"] if isinstance(item, dict)]
        if candidates:
            warnings.append("旧报告仅保留差异对象索引，整批对象范围未知")
    if not candidates and (event.symbol_id is not None or any(attrs.get(k) is not None for k in ("security_id", "symbol_id", "trade_date", "trade_date_from"))):
        candidates = [attrs]
    if not candidates:
        warnings.append("原审计缺少结构化对象索引，标的与所属交易日未知或不适用")
    results: list[HistoryObject] = []
    for index, item in enumerate(candidates):
        symbol_id = item.get("symbol_id")
        if symbol_id is None and len(candidates) == 1:
            symbol_id = event.symbol_id
        try:
            symbol_id = int(symbol_id) if symbol_id is not None else None
        except (TypeError, ValueError):
            symbol_id = None
        symbol = catalogue.get(symbol_id, {})
        security_id = _text(item.get("security_id"))
        if security_id and not re.match(r"^[A-Za-z][A-Za-z0-9_-]*[:.][^\s]+$", security_id):
            security_id = None
        market = _text(item.get("market"))
        code = _text(item.get("code") or item.get("symbol"))
        if not security_id and market and code:
            security_id = f"{market.upper()}:{code}"
        if not security_id:
            security_id = symbol.get("security_id")
        trade_date = _date(item.get("trade_date") or item.get("trade_date_from"))
        # A single event-level date is an explicit common scope for its objects.
        common_scope = trade_date is None and item is not attrs
        if common_scope:
            trade_date = _date(attrs.get("trade_date") or attrs.get("trade_date_from"))
        trade_to = _date(item.get("trade_date_to") or (attrs.get("trade_date_to") if common_scope else None))
        if trade_to and trade_date and trade_to < trade_date:
            trade_date = trade_to = None
        explicit_name = _text(item.get("security_name") or item.get("name"))
        raw_result = item.get("reinspection_status") or item.get("recheck_status") or item.get("execution_result") or item.get("result") or item.get("status")
        result_basis = "reinspection" if item.get("reinspection_status") is not None or item.get("recheck_status") is not None else "execution" if raw_result is not None else "unknown"
        if isinstance(raw_result, dict):
            raw_result = raw_result.get("status")
        if raw_result is None and item.get("kind"):
            raw_result = "unknown" if item["kind"] == "NOT_CHECKED" else "blocked"
        result = EXECUTION_MAP.get(str(raw_result).lower(), "unknown") if raw_result is not None else "unknown"
        results.append(HistoryObject(
            object_id=_text(item.get("object_id") or item.get("item_id")) or (f"{security_id}@{trade_date or 'unknown'}..{trade_to or ''}" if security_id else f"unknown-{event.id}-{index}"),
            security_id=security_id, symbol_id=symbol_id,
            market=market or symbol.get("market"), code=code or symbol.get("code"),
            name=explicit_name or symbol.get("name"),
            name_source="audit" if explicit_name else "current_catalogue" if symbol else "unknown",
            trade_date=trade_date, trade_date_to=trade_to,
            security_index_status="known" if security_id else "unknown",
            trade_date_index_status="known" if trade_date else "not_applicable" if attrs.get("confirmation_type") == "MODEL_BINDING_CONFIRMED" else "unknown",
            result=result, result_basis=result_basis, source_event_ids=[event.id], references=_references(item, event.id),
        ))
    if any(item.security_index_status == "unknown" for item in results):
        warnings.append("部分对象的证券市场身份缺失，不能参与标的筛选")
    if any(item.trade_date_index_status == "unknown" for item in results):
        warnings.append("部分对象的所属交易日索引缺失，不能参与交易日筛选")
    return results, warnings


def _groups(rows: list[DataGovernanceAuditEvent], attributes: dict[int, dict[str, Any]]) -> list[list[DataGovernanceAuditEvent]]:
    parent = {row.id: row.id for row in rows}

    def root(key: int) -> int:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    owners: dict[tuple[str, str], int] = {}
    for row in rows:
        attrs = attributes[row.id]
        keys = [(key, _text(attrs.get(key))) for key in GROUP_KEYS]
        keys.append(("correlation_id", row.correlation_id or _text(attrs.get("correlation_id"))))
        for kind, value in keys:
            if not value:
                continue
            key = ("batch_id" if kind in {"handling_batch_id", "review_batch_id"} else kind, value)
            if key in owners:
                a, b = root(row.id), root(owners[key])
                parent[max(a, b)] = min(a, b)
            else:
                owners[key] = row.id
    grouped: dict[int, list[DataGovernanceAuditEvent]] = {}
    for row in rows:
        grouped.setdefault(root(row.id), []).append(row)
    return list(grouped.values())


def _object_overall(objects: list[HistoryObject]) -> ExecutionResult:
    statuses = [obj.result for obj in objects]
    if not statuses or all(status == "unknown" for status in statuses):
        return "unknown"
    successes = statuses.count("success")
    failures = any(status in {"failed", "dependency_blocked", "partial_failure"} for status in statuses)
    if successes == len(statuses):
        return "success"
    if failures:
        return "partial_failure" if successes else "failed"
    if "running" in statuses:
        return "running"
    # An unknown object prevents a complete-success claim.
    if "unknown" in statuses:
        return "unknown"
    return "not_executed"


def _batch(rows: list[DataGovernanceAuditEvent], catalogue: dict[int, dict[str, Any]]) -> HistoryBatchDetail:
    rows = sorted(rows, key=lambda row: (_at(row.occurred_at), row.id))
    events: list[HistoryEvent] = []
    indexed: dict[tuple[Any, ...], HistoryObject] = {}
    warnings: list[str] = []
    last_object_result_position = -1
    last_execution_position = -1
    for position, row in enumerate(rows):
        material_warnings: list[str] = []
        before = _decoded(row.before_json, "变更前快照", material_warnings)
        after = _decoded(row.after_json, "变更后快照", material_warnings)
        attributes = _decoded(row.attributes_json, "结构化属性", material_warnings)
        attrs = _mapping(attributes)
        if attrs.get("source_timezone") != "UTC":
            material_warnings.append("原审计未标注源时区，按UTC数据库契约展示；原时间值另行保留，旧来源时区不能从文本推断")
        if any(isinstance(value, dict) and "_raw_repr" in value for value in (before, after, attrs)):
            material_warnings.append("旧审计仅保留序列化文本，相关结构化状态无法核验；不从该文本还原通过或证券日期索引")
        indexed_attrs = dict(attrs)
        # Narrow compatibility adapter for actual repair snapshots. Dates and
        # identities still come only from structured operation fields.
        if attrs.get("event_family") == "portfolio_inspection_repair" and isinstance(after, dict):
            if not any(isinstance(attrs.get(key), list) for key in OBJECT_KEYS) and isinstance(after.get("operations"), list):
                indexed_attrs["objects"] = [
                    {**item, "object_id": item.get("operation_id")}
                    for item in after["operations"] if isinstance(item, dict)
                ]
            if str(attrs.get("original_action")) == "REPAIR_TASK_REQUESTED":
                indexed_attrs.setdefault("execution_result", "not_executed")
            elif after.get("status"):
                status = after["status"]
                if after.get("ok_count") == 0 and (after.get("failed_count") or 0) > 0:
                    status = "failed"
                indexed_attrs.setdefault("execution_result", status)
        refs = _references([before, after, attrs], row.id)
        if row.correlation_id:
            refs.append(HistoryReference(kind="correlation", ref=row.correlation_id, source_event_id=row.id))
        # business_key remains visible verbatim in the event; it is not parsed
        # as a task/report/security reference without an explicit field.
        ev = HistoryEvent(
            id=row.id, action=str(attrs.get("original_action") or row.action), source_action=row.action,
            action_label={"MODEL_BINDING_CONFIRMED": "本人接受模型与策略变更", "RECONCILIATION_CONFIRMED": "本人确认对账修复",
                "ACCOUNT_BASELINE_ACCEPTED": "本人接受账户新覆盖起点", "INITIAL_COVERAGE_REVIEW_CONFIRMED": "本人审核启用账户新范围",
                "EVIDENCE_SUPPLEMENT_REQUESTED": "登记原执行来源补证", "EVIDENCE_SUPPLEMENT_REVIEWED": "本人审核原执行关联",
                "INSPECTION_GUIDE_CONFIRMED": "本人审核当前步骤证明并恢复"}.get(
                str(attrs.get("confirmation_type")), ACTION_LABELS.get(str(attrs.get("original_action") or row.action), "未登记操作")),
            occurred_at=_at(row.occurred_at), source_occurred_at=row.occurred_at, operator_id=row.operator_id,
            correlation_id=row.correlation_id, business_key=row.business_key,
            before=before, after=after, attributes=attributes, note=row.note,
            review_decision=_review(row, attrs), execution_result=_execution(row, indexed_attrs),
            references=_unique_refs(refs), material_warnings=material_warnings,
        )
        events.append(ev)
        if ev.execution_result != "unknown":
            last_execution_position = position
        objects, object_warnings = _objects(row, indexed_attrs, catalogue)
        if (any(obj.result != "unknown" for obj in objects)
                or (any(obj.result_basis == "reinspection" for obj in objects) and ev.execution_result not in {"not_executed", "running"})):
            last_object_result_position = position
        warnings.extend(material_warnings + object_warnings)
        for obj in objects:
            key = (obj.object_id, obj.security_id, obj.trade_date, obj.trade_date_to)
            previous = indexed.get(key)
            if previous:
                obj.source_event_ids = sorted(set(previous.source_event_ids + obj.source_event_ids))
                obj.references = _unique_refs(previous.references + obj.references)
            indexed[key] = obj
    objects = list(indexed.values())
    review = next((ev.review_decision for ev in reversed(events) if ev.review_decision != "unknown"), "unknown")
    execution = next((ev.execution_result for ev in reversed(events) if ev.execution_result != "unknown"), "unknown")
    object_result = _object_overall(objects)
    # Explicit per-object rechecks govern the full batch's result. A legacy
    # diffs-only index is incomplete and cannot prove total success.
    if (last_object_result_position >= 0 and last_object_result_position >= last_execution_position
            and not any("仅保留差异" in text for text in warnings)):
        execution = object_result
    refs = _unique_refs([ref for ev in events for ref in ev.references])
    configuration_only = all(ev.action in {"FACTOR_USAGE_APPLIED", "G6_ROLLOUT_STARTED", "G6_ROLLOUT_ROLLED_BACK"}
        or _mapping(ev.attributes).get("confirmation_type") in {"MODEL_BINDING_CONFIRMED", "INITIAL_COVERAGE_REVIEW_CONFIRMED"} for ev in events)
    for ref in refs:
        if ref.kind == "report" and ref.retention_status != "available":
            warnings.append(f"报告 {ref.ref} 的材料保留状态：{ref.retention_status}；保留原始引用与当时结论")
    return HistoryBatchDetail(
        batch_id=f"audit-{min(row.id for row in rows)}", portfolio_id=rows[0].portfolio_id,
        title=events[-1].action_label, occurred_at=events[-1].occurred_at,
        occurred_from=events[0].occurred_at,
        trade_date_index_status="known" if any(obj.trade_date for obj in objects) else "not_applicable" if configuration_only else "unknown",
        operators=list(dict.fromkeys(ev.operator_id for ev in events)),
        actions=list(dict.fromkeys(ev.action for ev in events)), review_decision=review,
        execution_result=execution, event_count=len(events), total_object_count=len(objects),
        matched_object_count=len(objects), matched_security_count=len({obj.security_id for obj in objects if obj.security_id}),
        objects=objects, matched_objects=objects, references=refs,
        index_warnings=list(dict.fromkeys(warnings)), events=events,
    )


def _load_batches(db: Session, portfolio_id: int, snapshot_event_id: int | None = None) -> tuple[list[HistoryBatchDetail], int]:
    # Portfolio scoping is applied before references, grouping, or object
    # lookup. Even a colliding correlation/batch ID cannot cross portfolios.
    with db.no_autoflush:
        anchor = db.scalar(select(func.max(DataGovernanceAuditEvent.id)).where(DataGovernanceAuditEvent.portfolio_id == portfolio_id)) or 0
        anchor = min(anchor, snapshot_event_id) if snapshot_event_id is not None else anchor
        rows = list(db.scalars(select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.portfolio_id == portfolio_id,
            DataGovernanceAuditEvent.id <= anchor,
        ).order_by(DataGovernanceAuditEvent.id)))
        attributes: dict[int, dict[str, Any]] = {}
        for row in rows:
            attributes[row.id] = _mapping(_decoded(row.attributes_json, "attributes", []))
        catalogue = _catalogue(db) if rows else {}
        batches = [_batch(group, catalogue) for group in _groups(rows, attributes)]
    return sorted(batches, key=lambda batch: (batch.occurred_at, int(batch.batch_id[6:])), reverse=True), anchor


def _relation_requested(filters: HistoryFilters) -> bool:
    return any((filters.issue_id, filters.correlation_id, filters.report_ref))


def _related(batch: HistoryBatchDetail, filters: HistoryFilters) -> bool:
    if not _relation_requested(filters):
        return True
    for ref in batch.references:
        if ref.kind == "issue" and filters.issue_id and ref.ref == filters.issue_id:
            return True
        if ref.kind == "correlation" and filters.correlation_id and ref.ref == filters.correlation_id:
            return True
        if ref.kind == "report" and filters.report_ref and ref.ref == filters.report_ref:
            if not filters.report_version or ref.version == filters.report_version:
                return True
    return False


def _object_matches(obj: HistoryObject, filters: HistoryFilters) -> bool:
    if filters.security_ids and obj.security_id not in filters.security_ids:
        return False
    if filters.trade_date_from or filters.trade_date_to:
        if obj.trade_date is None:
            return False
        # Intersect an explicitly indexed coverage interval. The security and
        # interval are always taken from this same object.
        if filters.trade_date_from and (obj.trade_date_to or obj.trade_date) < filters.trade_date_from:
            return False
        if filters.trade_date_to and obj.trade_date > filters.trade_date_to:
            return False
    return True


def _matches(batch: HistoryBatchDetail, filters: HistoryFilters) -> bool:
    # A time/action/operator filter matches a recorded event, while full
    # batch materials remain intact in the response.
    eligible_events = [ev for ev in batch.events if (
        (not filters.occurred_from or ev.occurred_at >= filters.occurred_from)
        and (not filters.occurred_to or ev.occurred_at <= filters.occurred_to)
        and (not filters.actions or ev.action in filters.actions)
        and (not filters.operator or ev.operator_id == filters.operator)
        and (not filters.actor_type or _mapping(ev.attributes).get("actor_type") == filters.actor_type)
        and (not filters.failure_code or _mapping(ev.attributes).get("failure_code") == filters.failure_code)
        and (filters.ai_assisted is None or (
            _mapping(ev.attributes).get("ai_assisted") is filters.ai_assisted
            or (filters.ai_assisted is True and _mapping(ev.attributes).get("actor_type") == "ai")
        ))
    )]
    if not eligible_events:
        return False
    if filters.review_decision and batch.review_decision != filters.review_decision:
        return False
    if filters.execution_result and batch.execution_result != filters.execution_result:
        return False
    if filters.q:
        haystack = json.dumps(batch.model_dump(mode="json"), ensure_ascii=False).casefold()
        if filters.q.casefold() not in haystack:
            return False
    matched = [obj for obj in batch.objects if _object_matches(obj, filters)]
    if (filters.security_ids or filters.trade_date_from or filters.trade_date_to) and not matched:
        return False
    batch.matched_objects = matched
    batch.matched_object_count = len(matched)
    batch.matched_security_count = len({obj.security_id for obj in matched if obj.security_id})
    return True


def select_inspection_history_batches(
    db: Session, portfolio_id: int, filters: HistoryFilters | None = None, *,
    snapshot_event_id: int | None = None, now: datetime | None = None,
) -> tuple[list[HistoryBatchDetail], int, HistoryFilters, str]:
    """One complete selection shared by pagination and evidence export."""
    filters = normalized_filters(filters or HistoryFilters(), now=now)
    batches, anchor = _load_batches(db, portfolio_id, snapshot_event_id)
    if filters.since_last_reconciliation:
        passed = [ev for batch in batches for ev in batch.events
            if ev.action == "RECONCILIATION_RESULT" and _mapping(ev.attributes).get("status") == "PASSED"]
        if not passed:
            raise ValueError("当前组合尚无真实通过的对账审计，无法定位上次对账时点；请使用明确的发生时间范围")
        filters.occurred_from = max(passed, key=lambda ev: (ev.occurred_at, ev.id)).occurred_at
    related = [batch for batch in batches if _related(batch, filters)]
    matched = [batch for batch in related if _matches(batch, filters)]
    relation_status = "not_requested"
    if _relation_requested(filters):
        relation_status = "no_related_history" if not related else "matched" if matched else "filtered_out"
    return matched, anchor, filters, relation_status


def query_inspection_history(
    db: Session, portfolio_id: int, filters: HistoryFilters | None = None, *,
    page: int = 1, page_size: int = 20, snapshot_event_id: int | None = None,
    now: datetime | None = None,
) -> HistoryPage:
    if page < 1 or not 1 <= page_size <= 200 or (snapshot_event_id is not None and snapshot_event_id < 0):
        raise ValueError("分页参数无效")
    matched, anchor, filters, relation_status = select_inspection_history_batches(
        db, portfolio_id, filters, snapshot_event_id=snapshot_event_id, now=now)
    total = len(matched)
    empty_reason = None
    if not total:
        empty_reason = "当前问题暂无关联历史" if relation_status == "no_related_history" else "当前筛选无匹配记录" if relation_status == "filtered_out" else "当前范围无匹配记录"
    return HistoryPage(
        items=matched[(page - 1) * page_size:page * page_size], total=total,
        page=page, page_size=page_size, page_count=(total + page_size - 1) // page_size,
        has_more=page * page_size < total, snapshot_event_id=anchor,
        filters=filters, relation_status=relation_status, empty_reason=empty_reason,
    )


def get_inspection_history_batch(
    db: Session, portfolio_id: int, batch_id: str, *,
    filters: HistoryFilters | None = None, snapshot_event_id: int | None = None,
) -> HistoryBatchDetail | None:
    if not re.fullmatch(r"audit-\d+", batch_id):
        return None
    batches, _ = _load_batches(db, portfolio_id, snapshot_event_id)
    for batch in batches:
        if batch.batch_id == batch_id:
            if filters:
                # Detail can highlight the current object query without
                # truncating objects, source events, or report references.
                batch.matched_objects = [obj for obj in batch.objects if _object_matches(obj, filters)]
                batch.matched_object_count = len(batch.matched_objects)
                batch.matched_security_count = len({obj.security_id for obj in batch.matched_objects if obj.security_id})
            return batch
    return None


def search_inspection_history_securities(db: Session, portfolio_id: int, *, q: str | None = None, limit: int = 50) -> HistorySecurityPage:
    batches, _ = _load_batches(db, portfolio_id)
    securities: dict[str, HistorySecurity] = {}
    for batch in batches:
        for obj in batch.objects:
            if not obj.security_id:
                continue
            if q and q.casefold() not in " ".join((obj.security_id, obj.code or "", obj.name or "")).casefold():
                continue
            securities.setdefault(obj.security_id, HistorySecurity(
                security_id=obj.security_id, market=obj.market, code=obj.code, name=obj.name,
            ))
    items = sorted(securities.values(), key=lambda item: item.security_id)
    return HistorySecurityPage(items=items[:limit], total=len(items))
