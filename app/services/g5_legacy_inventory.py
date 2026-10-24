"""Read-only HF-07 inventory of original G5 reports and actual adoption facts.

Current source configuration supplies context, never proof that any particular
G5 report was used. Inventory does not revoke a report, switch a source, replay
an engine, or change trading/account facts. Those are separate reviewed acts.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.g5_dual_run import G5DualRunReport
from app.models.portfolio import AUTO_TRADE_SOURCE_MODES, Portfolio
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.g5_dual_run_audit import validate_g5_report


POLICY_VERSION = "g5-legacy-inventory-v1"
_AUDIT_ACTIONS = {"G6_ROLLOUT_STARTED", "G6_ROLLOUT_ROLLED_BACK", "DATA_BLOCK_RESOLUTION"}
_SOURCE_SWITCH_TYPES = {"SOURCE_SWITCH", "AUTO_TRADE_SOURCE_SWITCH", "AUTO_TRADE_SOURCE_SWITCHED",
    "MEMBER_SOURCE_SWITCH", "G5_SOURCE_SWITCH", "AUTO_TRADE_SOURCE_MODE_CHANGED"}
_REFERENCE_KEYS = {"g5_report_id", "report_id", "audit_report_hash"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_json(value: str | None) -> Any:
    def reject_constant(token):
        raise ValueError(f"non-finite JSON constant: {token}")
    return json.loads(value if value is not None else "null", parse_constant=reject_constant)


def _storage_error(source: str, code: str, *, report_id: int | None = None,
                   detail: str | None = None, error_type: str | None = None) -> dict:
    return {"source": source, "code": code, "report_id": report_id,
            "detail": detail, "error_type": error_type}


def _read_rows(db: Session, model, *, columns=None, portfolio_id=None,
               audit=False, errors: list[dict], current_read: bool = False) -> list[dict]:
    table = model.__table__
    wanted = list(columns or table.c)
    try:
        inspector = inspect(db.connection())
        if not inspector.has_table(table.name):
            errors.append(_storage_error(table.name, "INVENTORY_TABLE_MISSING"))
            return []
        present = {col["name"] for col in inspector.get_columns(table.name)}
        missing = sorted(col.name for col in wanted if col.name not in present)
        if missing:
            errors.append(_storage_error(table.name, "INVENTORY_SCHEMA_INCOMPLETE",
                                         detail="missing columns: " + ",".join(missing)))
            return []
        statement = select(*wanted)
        if portfolio_id is not None:
            statement = statement.where(table.c.id == portfolio_id) if model is Portfolio else statement.where(table.c.portfolio_id == portfolio_id)
        if audit:
            statement = statement.where(table.c.action.in_(_AUDIT_ACTIONS))
        statement = statement.order_by(table.c.id)
        if current_read:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return [dict(row) for row in db.execute(statement).mappings()]
    except (SQLAlchemyError, TypeError, ValueError) as exc:
        errors.append(_storage_error(table.name, "INVENTORY_STORAGE_READ_FAILED", error_type=type(exc).__name__))
        return []


def _check_report(db: Session, row: dict, source_mode: str | None, errors: list[dict], *, current_read: bool) -> dict:
    # Construct an unattached typed view of persisted columns. Reading an ORM
    # identity could otherwise substitute an unrelated caller's dirty edits.
    record = G5DualRunReport(**row)
    local_errors: list[str] = []
    payload, matches, computed = None, None, None
    raw = row.get("report_json")
    raw_hash = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()
    try:
        payload = _strict_json(raw)
        if not isinstance(payload, dict):
            parse_status = "INVALID_STRUCTURE"
            local_errors.append("REPORT_JSON_NOT_OBJECT")
            payload = None
        else:
            parse_status = "PARSED"
            computed = _hash(payload)
            matches = computed == row["report_hash"]
            if not matches:
                local_errors.append("ORIGINAL_HASH_MISMATCH")
            if not isinstance(payload.get("provenance"), dict):
                local_errors.append("PROVENANCE_MISSING")
            if not isinstance(payload.get("status"), str):
                local_errors.append("REPORT_STATUS_MISSING")
            if payload.get("portfolio_id") != row["portfolio_id"]:
                local_errors.append("TYPED_PORTFOLIO_MISMATCH")
    except (TypeError, ValueError):
        parse_status = "INVALID_JSON"
        local_errors.append("REPORT_JSON_INVALID")
    checked: dict = {}
    if payload is not None:
        try:
            checked = validate_g5_report(db, record, portfolio_id=row["portfolio_id"], current_read=current_read)
            if not isinstance(checked, dict):
                raise TypeError("validator result is not an object")
            declared_errors = checked.get("g5_validation_errors") or []
            if isinstance(declared_errors, list):
                local_errors.extend(str(item) for item in declared_errors)
            else:
                local_errors.append("VALIDATOR_ERRORS_FORMAT_INVALID")
            for error in checked.get("storage_errors") or []:
                error = error if isinstance(error, dict) else {"code": "G5_VALIDATION_STORAGE_UNKNOWN"}
                errors.append(_storage_error("g5_validator", str(error.get("code") or "G5_VALIDATION_STORAGE_UNKNOWN"),
                    report_id=row["id"], detail=str(error.get("detail") or "") or None))
                local_errors.append("G5_VALIDATION_STORAGE_UNAVAILABLE")
        except SQLAlchemyError as exc:
            errors.append(_storage_error("g5_validator", "G5_VALIDATION_STORAGE_READ_FAILED",
                report_id=row["id"], error_type=type(exc).__name__))
            local_errors.append("G5_VALIDATION_STORAGE_READ_FAILED")
        except Exception as exc:
            errors.append(_storage_error("g5_validator", "G5_VALIDATOR_UNAVAILABLE",
                report_id=row["id"], error_type=type(exc).__name__))
            local_errors.append("G5_VALIDATOR_UNAVAILABLE")
    verified = checked.get("evidence_verified") is True and not local_errors
    if not verified and not local_errors:
        local_errors.append("G5_EVIDENCE_NOT_VERIFIED")
    payload_status = parse_status
    if payload is not None:
        status = payload.get("status")
        payload_status = status if isinstance(status, str) else "MISSING" if status is None else "INVALID_STATUS"
    return {"report_id": row["id"], "portfolio_id": row["portfolio_id"],
        "original_hash": row["report_hash"], "original_hash_matches": matches,
        "computed_payload_hash": computed, "raw_payload_sha256": raw_hash,
        "parse_status": parse_status, "payload_status": payload_status,
        "validation_status": checked.get("status", "INVALID"), "evidence_verified": verified,
        "eligible": verified and checked.get("g5_eligible_for_g6") is True,
        "validation_errors": list(dict.fromkeys(local_errors)), "references": [],
        "current_source_mode": source_mode, "review_status": "valid_reference" if verified else "requires_review",
        "historically_adopted": False, "reference_status": "no_known_reference"}


def _walk(value: Any, path: str):
    if isinstance(value, dict):
        yield path, value
        for key in sorted(value):
            if isinstance(value[key], (dict, list)):
                yield from _walk(value[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def _source_mode(value: dict) -> str | None:
    mode = value.get("auto_trade_source_mode")
    return mode if isinstance(mode, str) and mode in AUTO_TRADE_SOURCE_MODES else None


def _is_source_switch(parts: dict[str, dict]) -> bool:
    for part in parts.values():
        for _, node in _walk(part, ""):
            if str(node.get("confirmation_type", node.get("event_type", ""))).upper() in _SOURCE_SWITCH_TYPES:
                return True
            if (isinstance(node.get("from_source_mode"), str) and node["from_source_mode"] in AUTO_TRADE_SOURCE_MODES
                    and isinstance(node.get("to_source_mode"), str) and node["to_source_mode"] in AUTO_TRADE_SOURCE_MODES):
                return True
    before, after = _source_mode(parts["before"]), _source_mode(parts["after"])
    return before is not None and after is not None and before != after


def _positive_id(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and len(value) <= 19 and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    return None


def _audit_facts(row: dict) -> tuple[dict | None, list[dict]]:
    parts, malformed = {}, []
    for field in ("before", "after", "attributes"):
        try:
            parsed = _strict_json(row.get(field + "_json"))
            if parsed is not None and not isinstance(parsed, dict):
                raise ValueError("audit payload not object")
            parts[field] = parsed or {}
        except (TypeError, ValueError):
            parts[field] = {}
            malformed.append(field)
    action = row["action"]
    if action == "DATA_BLOCK_RESOLUTION" and not _is_source_switch(parts):
        if not malformed:
            return None, []
        return {"audit_id": row["id"], "action": action, "portfolio_id": row["portfolio_id"],
            "occurred_at": row["occurred_at"], "relation": "unproven",
            "reason_code": "AUDIT_CLASSIFICATION_UNREADABLE", "unreadable_fields": malformed,
            "audit_content_hash": _hash(row)}, []
    base = {"audit_id": row["id"], "action": action, "portfolio_id": row["portfolio_id"],
        "occurred_at": row["occurred_at"], "relation": "unproven",
        "from_source_mode": _source_mode(parts["before"]), "to_source_mode": _source_mode(parts["after"]),
        "audit_content_hash": _hash(row), "unreadable_fields": malformed}
    references: dict[tuple, dict] = {}
    for part_name, part in parts.items():
        for path, node in _walk(part, part_name):
            # A generic report_id has G5 meaning only inside these explicitly
            # admitted actions and when it is a positive typed/numeric ID.
            # Reconciliation UUIDs and arbitrary text must not become G5 IDs.
            rid = _positive_id(node.get("g5_report_id", node.get("report_id")))
            original_id = node.get("g5_report_id", node.get("report_id"))
            rhash = node.get("audit_report_hash")
            valid_hash = rhash if isinstance(rhash, str) and re.fullmatch(r"[a-f0-9]{64}", rhash) else None
            explicit_bad_id = "g5_report_id" in node and original_id is not None and rid is None
            explicit_bad_hash = "audit_report_hash" in node and rhash is not None and valid_hash is None
            if rid is None and valid_hash is None and not explicit_bad_id and not explicit_bad_hash:
                continue
            key = (rid, valid_hash, str(original_id) if explicit_bad_id else None, str(rhash) if explicit_bad_hash else None)
            ref = references.setdefault(key, {**base, "relation": "explicit", "report_id": rid,
                "audit_report_hash": valid_hash, "reference_paths": [], "historical_adoption": False})
            ref["reference_paths"].append(path)
            if explicit_bad_id or explicit_bad_hash:
                ref.update(reason_code="G5_REFERENCE_FORMAT_INVALID", original_report_id=original_id,
                           original_reference_hash=rhash)
            failed = str(parts["attributes"].get("status", "")).upper() in {"FAILED", "BLOCKED", "REJECTED"}
            if not failed and part_name in {"after", "attributes"} and (
                    action == "G6_ROLLOUT_STARTED" or (action == "DATA_BLOCK_RESOLUTION"
                    and base["from_source_mode"] is not None and base["to_source_mode"] is not None
                    and base["from_source_mode"] != base["to_source_mode"])):
                ref["historical_adoption"] = True
    if not references:
        base["reason_code"] = "G5_REFERENCE_NOT_RECORDED"
    else:
        base["relation"] = "explicit"
        base["reference_candidates"] = [{key: ref.get(key) for key in (
            "report_id", "audit_report_hash", "reference_paths", "reason_code")}
            for ref in references.values()]
    return base, list(references.values())


def build_g5_legacy_inventory(db: Session, *, portfolio_id: int | None = None,
                              current_read: bool = False) -> dict[str, Any]:
    """Read original facts; write-side callers may request fresh locking reads."""
    generated_at = datetime.now(timezone.utc).isoformat()
    with db.no_autoflush:
        errors: list[dict] = []
        try:
            from app.services.g5_admission_governance import TABLES as governance_tables
            missing_governance = sorted(set(governance_tables) - set(inspect(db.connection()).get_table_names()))
            if missing_governance:
                errors.append(_storage_error("g5_admission_governance", "G5_GOVERNANCE_STORAGE_UNAVAILABLE",
                    detail="missing tables: " + ",".join(missing_governance)))
        except (ImportError, SQLAlchemyError) as exc:
            errors.append(_storage_error("g5_admission_governance", "G5_GOVERNANCE_STORAGE_UNAVAILABLE",
                error_type=type(exc).__name__))
        portfolios = _read_rows(db, Portfolio, columns=[Portfolio.__table__.c.id,
            Portfolio.__table__.c.name, Portfolio.__table__.c.auto_trade_source_mode],
            portfolio_id=portfolio_id, errors=errors, current_read=current_read)
        report_rows = _read_rows(db, G5DualRunReport, portfolio_id=portfolio_id, errors=errors, current_read=current_read)
        audits = _read_rows(db, DataGovernanceAuditEvent, portfolio_id=portfolio_id, audit=True, errors=errors, current_read=current_read)
        modes = {row["id"]: row["auto_trade_source_mode"] for row in portfolios}
        reports = [_check_report(db, row, modes.get(row["portfolio_id"]), errors, current_read=current_read) for row in report_rows]
        by_id = {report["report_id"]: report for report in reports}
        by_hash = {report["original_hash"]: report for report in reports}
        unresolved, relevant_audits = [], []
        for row in audits:
            fact, references = _audit_facts(row)
            if fact is None:
                continue
            relevant_audits.append(fact)
            if not references:
                unresolved.append(fact)
            for ref in references:
                report = by_id.get(ref["report_id"]) if ref["report_id"] is not None else by_hash.get(ref["audit_report_hash"])
                if ref.get("reason_code"):
                    unresolved.append(ref)
                    continue
                if report is None:
                    unresolved.append({**ref, "reason_code": "G5_REPORT_REFERENCE_UNRESOLVED"})
                    continue
                if ref["portfolio_id"] != report["portfolio_id"]:
                    unresolved.append({**ref, "reason_code": "G5_REFERENCE_PORTFOLIO_MISMATCH",
                                       "referenced_report_portfolio_id": report["portfolio_id"]})
                    continue
                if ref["audit_report_hash"] is not None and ref["audit_report_hash"] != report["original_hash"]:
                    unresolved_ref = {**ref, "reason_code": "G5_REFERENCE_HASH_MISMATCH", "reference_valid": False}
                    unresolved.append(unresolved_ref)
                    report["references"].append(unresolved_ref)
                    report["review_status"] = "requires_review"
                    report["reference_status"] = "explicit_reference_unresolved"
                    continue
                ref["reference_valid"] = True
                ref["report_id"] = report["report_id"]
                ref["report_original_hash"] = report["original_hash"]
                existing = next((item for item in report["references"] if item["audit_id"] == ref["audit_id"]
                                 and item.get("reference_valid") is True), None)
                if existing:
                    existing["reference_paths"] = sorted(set(existing["reference_paths"] + ref["reference_paths"]))
                    existing["historical_adoption"] |= ref["historical_adoption"]
                    if ref["audit_report_hash"] is not None:
                        existing["audit_report_hash"] = ref["audit_report_hash"]
                else:
                    report["references"].append(ref)
                report["historically_adopted"] |= ref["historical_adoption"]
                report["reference_status"] = "explicit_reference"
        for report in reports:
            conflicting_audits = {ref["audit_id"] for ref in report["references"]
                                  if ref.get("reason_code") == "G5_REFERENCE_HASH_MISMATCH"}
            for ref in report["references"]:
                if ref["audit_id"] in conflicting_audits:
                    ref["historical_adoption"] = False
                    ref["reference_conflict"] = True
            report["historically_adopted"] = any(ref["historical_adoption"] for ref in report["references"])
            if conflicting_audits:
                report["reference_status"] = "explicit_reference_unresolved"
        affected = []
        for pid in sorted(set(modes) | {report["portfolio_id"] for report in reports} |
                          {fact["portfolio_id"] for fact in relevant_audits if fact["portfolio_id"] is not None}):
            own_reports = [r for r in reports if r["portfolio_id"] == pid]
            explicit = [r for r in own_reports if r["references"]]
            adopted = [r for r in own_reports if r["historically_adopted"]]
            own_facts = [a for a in relevant_audits if a["portfolio_id"] == pid]
            # Present configuration cannot identify a report or prove a past
            # transition. Preserve that uncertainty at portfolio scope.
            latest_fact = own_facts[-1] if own_facts else None
            latest_explicit_adoption = bool(latest_fact and latest_fact.get("to_source_mode") == modes.get(pid)
                and any(ref["audit_id"] == latest_fact["audit_id"] and ref["historical_adoption"] and ref.get("reference_valid") is True
                        for report in own_reports for ref in report["references"]))
            if modes.get(pid) == "members_only" and not latest_explicit_adoption:
                unresolved.append({"portfolio_id": pid, "audit_id": None, "action": "CURRENT_SOURCE_MODE",
                    "occurred_at": None, "relation": "unproven", "report_id": None,
                    "current_source_mode": modes[pid], "reason_code": "CURRENT_MODE_HAS_NO_PROVEN_G5_REFERENCE"})
            own_unresolved = [r for r in unresolved if r["portfolio_id"] == pid]
            invalid = [r["report_id"] for r in own_reports if not r["evidence_verified"]]
            if own_reports or own_facts or own_unresolved:
                affected.append({"portfolio_id": pid, "current_source_mode": modes.get(pid),
                    "report_ids": [r["report_id"] for r in own_reports], "invalid_report_ids": invalid,
                    "explicit_reference_report_ids": [r["report_id"] for r in explicit],
                    "historically_adopted_report_ids": [r["report_id"] for r in adopted],
                    "audit_ids": [r["audit_id"] for r in own_facts], "unresolved_reference_count": len(own_unresolved),
                    "unproven_reference_count": sum(r["relation"] == "unproven" for r in own_unresolved),
                    "requires_review": bool(invalid or own_unresolved)})
        if portfolio_id is not None and not portfolios and not any(e["source"] == "portfolios" for e in errors):
            errors.append(_storage_error("portfolios", "INVENTORY_PORTFOLIO_NOT_FOUND"))
        rollback = {"original_reports": [{key: report[key] for key in (
            "report_id", "portfolio_id", "original_hash", "raw_payload_sha256")} for report in reports],
            "audit_references": relevant_audits, "policy": "append_only_review_required",
            "trading_facts_modified": False, "source_modes_modified": False,
            "statement": "本盘点保留原始报告、原哈希与真实审计引用；不失效报告、不撤销历史已采用方案、不修改余额、持仓、订单、成交或运行状态。"}
        result = {"policy_version": POLICY_VERSION, "portfolio_id": portfolio_id,
            "report_count": len(reports), "invalid_report_count": sum(not r["evidence_verified"] for r in reports),
            "affected_portfolios": affected, "reports": reports, "unresolved_references": unresolved,
            "rollback_material": rollback, "storage_errors": errors, "reviewable": not errors,
            "counts_complete": not errors,
            "limitations": ["当前来源模式不能证明某份报告被采用；历史采用仅依据明确的真实审计引用。",
                "G7 查询/扩大范围判定未持久化独立采用记录时，本盘点不能还原其历史查询或批准结果。"]}
        return {"snapshot_id": _hash(result), "generated_at": generated_at, **result}
