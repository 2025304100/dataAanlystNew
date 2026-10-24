"""Scoped, read-only historical materials and reproducible evidence packages.

An audit reference authorizes locating that source, never substituting a newly
computed report. Mutable retained rows are explicitly distinguished from the
original frozen audit snapshot. Missing sources remain missing.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.orm import Session

from app.schemas.portfolio_inspection_history import HistoryFilters, HistoryMaterial, HistoryReference
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.portfolio_inspection_history import _references, select_inspection_history_batches


_SECRET_KEY = re.compile(r"password|passwd|secret|access[_-]?token|refresh[_-]?token|api[_-]?key|authorization|cookie|connection[_-]?string|database[_-]?url", re.I)
_SECRET_VALUE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
_REDACTED = "[已脱敏]"


def _json(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def redact_history_material(value: Any) -> Any:
    """Conservative credential redaction, including nested JSON source columns."""
    if isinstance(value, dict):
        return {key: _REDACTED if _SECRET_KEY.search(str(key)) else redact_history_material(
            _json(child) if str(key).endswith("_json") else child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_history_material(child) for child in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(lambda match: (match.group(1) or match.group(2) or "") + _REDACTED, value)
    return value


def _source_row(db: Session, table_name: str, row_id: str, portfolio_id: int) -> dict[str, Any] | None:
    # Table names originate only from the fixed adapters below; never from a URL.
    if not inspect(db.connection()).has_table(table_name):
        return None
    table = Table(table_name, MetaData(), autoload_with=db.connection())
    if "id" not in table.c or "portfolio_id" not in table.c:
        return None
    row = db.execute(select(table).where(table.c.id == row_id, table.c.portfolio_id == portfolio_id)).mappings().first()
    return dict(row) if row else None


def get_inspection_history_material(
    db: Session, portfolio_id: int, *, source_event_id: int, kind: str, ref: str,
    version: str | None = None, snapshot_event_id: int | None = None,
) -> HistoryMaterial | None:
    """Resolve only an exact structured reference retained by a scoped event.

    None means the reference itself does not belong to this portfolio/event.
    An existing reference whose source was deleted returns a material with an
    explicit missing state; it is never rebuilt from current account values.
    """
    with db.no_autoflush:
        event = db.scalar(select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.id == source_event_id,
            DataGovernanceAuditEvent.portfolio_id == portfolio_id,
        ))
        if event is None or (snapshot_event_id is not None and event.id > snapshot_event_id):
            return None
        before, after, attrs = map(_json, (event.before_json, event.after_json, event.attributes_json))
        references = _references([before, after, attrs], event.id)
        if event.correlation_id:
            references.append(HistoryReference(kind="correlation", ref=event.correlation_id, source_event_id=event.id))
        reference = next((item for item in references if (item.kind, item.ref, item.version) == (kind, ref, version)), None)
        if reference is None:
            return None
        original = {"event_id": event.id, "action": event.action, "occurred_at_utc": event.occurred_at,
            "operator_id": event.operator_id, "note": event.note, "before": before, "after": after, "attributes": attrs}
        material = HistoryMaterial(portfolio_id=portfolio_id, reference=reference, status="unsupported",
            material_kind="reference_only", title="原始关联索引",
            explanation="原审计保留此引用；该来源尚无可验证的只读适配器，不以最新材料替换。",
            original_audit=redact_history_material(original), missing_sources=[f"{kind}:{ref}"])
        # Explicit retained-state facts take precedence over a similarly named
        # current record. Inaccessible/archived evidence must not leak via a fallback.
        if reference.retention_status in {"archived", "inaccessible", "missing"}:
            material.status = reference.retention_status
            material.explanation = "原审计注明材料已归档、不可访问或缺失；当时决定和原始引用继续保留。"
            return material
        if kind in {"report", "baseline"} and inspect(db.connection()).has_table("portfolio_account_baselines"):
            from app.models.portfolio_reconciliation_coverage import PortfolioAccountBaseline, PortfolioReconciliationReport
            if kind == "baseline":
                row = db.scalar(select(PortfolioAccountBaseline).where(
                    PortfolioAccountBaseline.id == ref, PortfolioAccountBaseline.portfolio_id == portfolio_id))
                if row:
                    if _hash(_json(row.snapshot_json)) != row.content_hash:
                        material.status = "partial"
                        material.explanation = "原账户基线快照与保存指纹不一致，不能作为可信基线；原审计及缺口继续保留。"
                        material.missing_sources = ["可核验的原账户基线内容指纹"]
                        return material
                    material.status, material.material_kind = "available", "immutable_baseline"
                    material.title, material.explanation = "当时接受的账户基线", "按确切基线ID读取接受后不可变的快照；旧期未知结论仍然保留。"
                    material.data = {"id": row.id, "kind": row.kind, "as_of_at_utc": row.as_of_at,
                        "coverage_start_trade_date": row.coverage_start_trade_date,
                        "snapshot": _json(row.snapshot_json), "source_refs": _json(row.source_refs_json),
                        "history_gaps": _json(row.history_gaps_json), "accepted_audit_id": row.accepted_audit_id}
                    material.content_hash = row.content_hash
            elif inspect(db.connection()).has_table("portfolio_reconciliation_reports"):
                row = db.scalar(select(PortfolioReconciliationReport).join(PortfolioAccountBaseline,
                    PortfolioReconciliationReport.baseline_id == PortfolioAccountBaseline.id).where(
                    PortfolioReconciliationReport.id == ref, PortfolioAccountBaseline.portfolio_id == portfolio_id))
                if row and (version is None or version == str(row.report_version)):
                    material.status, material.material_kind = "available", "immutable_report"
                    material.title, material.explanation = "原版本账户核对报告", "按确切报告ID及版本读取不可变记录；未调用当前重检或替换为最新报告。"
                    material.data = {"id": row.id, "report_version": row.report_version, "evidence_version": row.evidence_version,
                        "baseline_id": row.baseline_id, "checks": _json(row.checks_json), "findings": _json(row.findings_json),
                        "source_refs": _json(row.source_refs_json), "verdict": row.verdict, "closure_type": row.closure_type,
                        "policy_version": row.policy_version, "calendar_version": row.calendar_version,
                        "audit_event_id": row.audit_event_id, "created_at_utc": row.created_at}
                    material.content_hash = row.evidence_hash
        if material.status == "available":
            material.data = redact_history_material(material.data)
            material.missing_sources = []
            return material
        def explicit_g5_reference(value: Any) -> bool:
            if isinstance(value, dict):
                return (str(value.get("g5_report_id", "")) == ref
                    or str(value.get("audit_report_id", "")) == ref
                    or str(value.get("confirmation_type", "")).startswith("G5_ADMISSION_")
                    or any(explicit_g5_reference(child) for child in value.values()))
            return isinstance(value, list) and any(explicit_g5_reference(child) for child in value)
        if kind == "report" and explicit_g5_reference([before, after, attrs]):
            row = _source_row(db, "g5_dual_run_reports", ref, portfolio_id)
            if row and (version is None or version == row.get("report_hash")):
                payload = _json(row.get("report_json"))
                material.status = "available" if isinstance(payload, dict) and _hash(payload) == row.get("report_hash") else "partial"
                material.material_kind, material.title = "immutable_report", "原版本双跑报告留存材料"
                material.explanation = "只读返回确切原报告JSON与保存指纹；这不重新验证来源真实性，不代表当前准入有效，也不会改变任何采用或失效记录。"
                material.data = redact_history_material({"id": row["id"], "report_hash": row.get("report_hash"),
                    "original_report": payload, "start_date": row.get("start_date"), "end_date": row.get("end_date"),
                    "integrity_matches_saved_hash": material.status == "available"})
                material.content_hash = row.get("report_hash")
                material.missing_sources = [] if material.status == "available" else ["原报告内容与保存指纹无法一致核验"]
                return material
        if kind == "report":
            # A legacy audit can retain its report excerpt without retaining the
            # complete report. Return that frozen excerpt with an explicit gap.
            material.status, material.material_kind = "partial", "audit_snapshot"
            material.title = "原报告索引及当时审计快照"
            material.explanation = "原始审计快照可回看，完整原报告未在可验证的不可变来源中找到；保留原结论，不能称为完整报告或重新通过。"
            material.data = redact_history_material({"before": before, "after": after, "attributes": attrs})
            material.content_hash = _hash(material.data)
            material.missing_sources = [f"完整原报告 {ref}" + (f" / 版本 {version}" if version else "")]
        elif kind in {"run", "order", "trade", "ledger", "snapshot", "evidence", "source_record"}:
            adapters = {"run": "decision_runs", "order": "sim_orders", "trade": "sim_trades", "ledger": "cash_ledger",
                        "snapshot": "strategy_execution_snapshots", "evidence": "decision_evidence"}
            table_name = adapters.get(kind)
            # Only the explicit source_record grammar names a table, and only
            # from this whitelist. No path reads, SQL text, or arbitrary URLs.
            row_id = ref
            if kind in {"source_record", "evidence"}:
                parts = ref.split(":", 1)
                if len(parts) == 2 and parts[0] in adapters.values():
                    table_name, row_id = parts
            row = _source_row(db, table_name, row_id, portfolio_id) if table_name else None
            material.status, material.material_kind = ("available", "retained_source") if row else ("missing", "reference_only")
            material.title = "当前留存的关联来源" if row else "关联来源未保留"
            material.explanation = ("这是当前留存的确切来源行；它与当时审计快照分别展示，不声称是当时冻结的完整报告。" if row else
                "当前组合未找到此确切来源，可能已删除、未迁移或来源不支持；未从备注推测或生成替代记录。")
            material.data = redact_history_material(row)
            material.content_hash = _hash(material.data) if row else None
            material.missing_sources = [] if row else [f"{table_name or kind}:{row_id}"]
        elif kind == "task":
            # async_tasks has no portfolio FK. Check the retained typed payload
            # before returning any of its data, even though the audit is scoped.
            if inspect(db.connection()).has_table("async_tasks"):
                table = Table("async_tasks", MetaData(), autoload_with=db.connection())
                task_id = ref.removeprefix("async_task:")
                row = db.execute(select(table).where(table.c.id == task_id)).mappings().first()
                payload = _json(row.get("payload_json")) if row else None
                if isinstance(payload, dict) and str(payload.get("portfolio_id")) == str(portfolio_id):
                    material.status, material.material_kind = "available", "retained_source"
                    material.title, material.explanation = "当前留存的关联任务", "任务当前留存状态与当时审计快照分开显示，重试结果不会改写原快照。"
                    material.data = redact_history_material(dict(row)); material.content_hash = _hash(material.data)
                    material.missing_sources = []
                else:
                    material.status = "missing"
        return material


def export_inspection_history_evidence(
    db: Session, portfolio_id: int, filters: HistoryFilters | None = None, *,
    snapshot_event_id: int | None = None, actor: str,
) -> dict[str, Any]:
    """Export all matching batches at the list's event watermark, not one page."""
    batches, anchor, applied, relation = select_inspection_history_batches(
        db, portfolio_id, filters, snapshot_event_id=snapshot_event_id)
    refs = {(ref.kind, ref.ref, ref.version, ref.source_event_id): ref for batch in batches for ref in batch.references}
    materials = [get_inspection_history_material(db, portfolio_id, source_event_id=ref.source_event_id,
        kind=ref.kind, ref=ref.ref, version=ref.version, snapshot_event_id=anchor) for ref in refs.values()]
    retained = [material.model_dump(mode="json") for material in materials if material is not None]
    coverage_archive: dict[str, Any] = {"available": False, "missing_sources": []}
    coverage_tables = ("portfolio_account_baselines", "portfolio_reconciliation_cursors", "portfolio_reconciliation_days", "portfolio_reconciliation_reports")
    present = set(inspect(db.connection()).get_table_names())
    if not all(name in present for name in coverage_tables):
        coverage_archive["missing_sources"] = [name for name in coverage_tables if name not in present]
    else:
        tables = {name: Table(name, MetaData(), autoload_with=db.connection()) for name in coverage_tables}
        baselines, cursors, days, reports = (tables[name] for name in coverage_tables)
        retained_baselines = [dict(row) for row in db.execute(select(baselines).where(
            baselines.c.portfolio_id == portfolio_id, baselines.c.accepted_audit_id <= anchor)).mappings()]
        retained_cursors = [dict(row) for row in db.execute(select(cursors).where(cursors.c.portfolio_id == portfolio_id)).mappings()]
        retained_days = [dict(row) for row in db.execute(select(days).where(days.c.cursor_id.in_([row["id"] for row in retained_cursors]))).mappings()]
        retained_reports = [dict(row) for row in db.execute(select(reports).where(
            reports.c.baseline_id.in_([row["id"] for row in retained_baselines]), reports.c.audit_event_id <= anchor)).mappings()]
        coverage_archive = {"available": True, "scope": "persisted_current_and_prior_generations",
            "baselines": [{key: value for key, value in row.items() if key not in {"acceptance_key", "request_hash"}} for row in retained_baselines],
            "current_cursors_at_export": retained_cursors, "current_work_queue_at_export": retained_days, "reports": retained_reports,
            "mutable_state_read_at_utc": datetime.now(timezone.utc).isoformat(),
            "watermark_scope": "原基线与不可变报告按审计水位冻结；当前游标/工作队列为导出时留存状态，不替代原快照",
            "explanation": "保留的账户基线、旧期缺口及各代原报告；未重新推算缺失历史，未执行当前重检。"}
    payload = redact_history_material({"schema_version": "portfolio-inspection-evidence-v1", "portfolio_id": portfolio_id,
        "snapshot_event_id": anchor, "filters": applied.model_dump(mode="json"), "relation_status": relation,
        "batch_count": len(batches), "batches": [batch.model_dump(mode="json") for batch in batches],
        "materials": retained,
        "account_coverage_archive": coverage_archive,
        "missing_evidence": [{"reference": material["reference"], "status": material["status"],
                              "missing_sources": material["missing_sources"]} for material in retained if material["status"] != "available"],
        "exceptions_and_unknowns": [{"batch_id": batch.batch_id, "warnings": batch.index_warnings,
            "review_decision": batch.review_decision, "execution_result": batch.execution_result}
            for batch in batches if batch.index_warnings or batch.execution_result != "success"],
        "access": {"portfolio_access_checked": True, "requested_by": actor, "credential_redaction": True,
                   "scope": "current_portfolio", "authorization": "existing_portfolio_access_policy"},
        "read_only": True})
    return {"manifest": {"exported_at": datetime.now(timezone.utc).isoformat(), "content_sha256": _hash(payload),
            "hash_scope": "redacted_payload", "read_only": True}, "payload": payload}
