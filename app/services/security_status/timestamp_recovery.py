"""Recover legacy timestamp precision only from intact original import evidence.

This does not relax normal import/constraint checks or certify a provider's
claims. It requires the original full request and a separately retained digest.
No receipt, constraint chunk, status value, row ID or batch ID is rewritten.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.security_status import SecurityStatusDaily
from app.models.symbol import Symbol
from app.schemas.security_status import SecurityStatusFileRequest
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services.security_status import local_import as imports


class TimestampRecoveryError(ValueError):
    pass


def _hash(value) -> str:
    return hashlib.sha256(imports._canonical(value).encode("utf-8")).hexdigest()


def _snapshot(row) -> dict:
    return {column.name: value.isoformat() if isinstance(value, (date, datetime)) else value
            for column in row.__table__.columns for value in [getattr(row, column.name)]}


def _read(db, statement, lock):
    statement = statement.execution_options(populate_existing=True)
    return db.scalars(statement.with_for_update() if lock else statement).all()


def _original_rows(db: Session, payload: SecurityStatusFileRequest) -> dict:
    reader = csv.DictReader(io.StringIO(payload.csv_text.lstrip("\ufeff")), strict=True)
    headers = reader.fieldnames
    if (not headers or len(headers) != len(set(headers))
            or not set(imports.COLUMNS).issubset(headers)
            or not set(headers).issubset((*imports.COLUMNS, *imports.EXECUTION_COLUMNS))):
        raise TimestampRecoveryError("Original file headers are incomplete or ambiguous")
    originals = {}
    names = {}
    for raw in reader:
        if len(originals) >= imports.MAX_ROWS or None in raw or any(value is None for value in raw.values()):
            raise TimestampRecoveryError("Original file row count or field count is invalid")
        value = {key: value.strip() for key, value in raw.items()}
        name = value["symbol"]
        if name not in names:
            symbols = db.scalars(select(Symbol.id).where(Symbol.symbol == name)).all()
            if len(symbols) != 1:
                raise TimestampRecoveryError("Original symbol identity is missing or ambiguous")
            names[name] = symbols[0]
        if any(value[flag] not in {"0", "1"} for flag in imports.FLAGS):
            raise TimestampRecoveryError("Original status flags are invalid")
        day = date.fromisoformat(value["trade_date"])
        key = (names[name], day)
        if key in originals:
            raise TimestampRecoveryError("Original file contains duplicate symbol dates")
        originals[key] = {
            "symbol_id": key[0], "trade_date": day,
            **{flag: value[flag] == "1" for flag in imports.FLAGS},
            "listing_date": date.fromisoformat(value["listing_date"]),
            "delisting_date": date.fromisoformat(value["delisting_date"]) if value["delisting_date"] else None,
            "execution": imports._execution_values(value),
        }
    return originals


def _build_plan(db: Session, payload: SecurityStatusFileRequest, digest: str, *, lock: bool) -> tuple[dict, list]:
    if not re.fullmatch(r"[0-9a-f]{64}", digest or "") or imports._fingerprint(payload) != digest:
        raise TimestampRecoveryError("Original request does not match the separately retained content SHA256")
    batch_id = f"status_csv_{digest[:24]}"
    receipts = _read(db, select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.action == "DATA_BLOCK_RESOLUTION",
        DataGovernanceAuditEvent.business_key == batch_id).order_by(DataGovernanceAuditEvent.id), lock)
    if len(receipts) != 1:
        raise TimestampRecoveryError("Original receipt is missing or duplicated; recovery is ambiguous")
    receipt = json.loads(receipts[0].after_json or "{}")
    if not isinstance(receipt, dict):
        raise TimestampRecoveryError("Original receipt must be an object")
    expected = payload.source_updated_at.astimezone(timezone.utc).replace(tzinfo=None)
    receipt_time = datetime.fromisoformat(receipt["source_updated_at"])
    if receipt_time.tzinfo is None:
        raise TimestampRecoveryError("Original receipt timestamp must declare its time zone")
    if (receipt.get("kind") != "security_status_import" or receipt.get("batch_id") != batch_id
            or receipt.get("content_sha256") != digest
            or any(receipt.get(field) != getattr(payload, field) for field in ("source_name", "source_reference", "source_version", "filename"))
            or receipt_time.astimezone(timezone.utc).replace(tzinfo=None) != expected
            or imports.is_unverified_status_source(payload.source_name)):
        raise TimestampRecoveryError("Original receipt metadata does not match the original request")
    originals = _original_rows(db, payload)
    if not originals or len(originals) != receipt.get("total_rows"):
        raise TimestampRecoveryError("Original file row count does not match its receipt")
    hashes = receipt.get("execution_constraint_hashes")
    if not isinstance(hashes, list) or not hashes or len(hashes) > imports.MAX_ROWS:
        raise TimestampRecoveryError("Original status fingerprints are unavailable; timestamps cannot be guessed")
    chunks = _read(db, select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.action == "DATA_BLOCK_RESOLUTION",
        DataGovernanceAuditEvent.business_key.like(f"{batch_id}:execution:%"))
        .order_by(DataGovernanceAuditEvent.id), lock)
    if len(chunks) != len(hashes) or len({item.business_key for item in chunks}) != len(chunks):
        raise TimestampRecoveryError("Original constraint chunks are missing, duplicated or unexpected")
    by_key = {item.business_key: item for item in chunks}
    constraints = {}
    for index, expected_hash in enumerate(hashes):
        event = by_key.get(f"{batch_id}:execution:{index}")
        if event is None or len((event.after_json or "").encode("utf-8")) > imports.AUDIT_CHUNK_BYTES:
            raise TimestampRecoveryError("Original constraint chunk is missing or oversized")
        chunk = json.loads(event.after_json or "{}")
        if not isinstance(chunk, dict):
            raise TimestampRecoveryError("Original constraint chunk must be an object")
        if (_hash(chunk) != expected_hash or chunk.get("kind") != "security_status_execution_constraints"
                or chunk.get("batch_id") != batch_id or chunk.get("content_sha256") != digest):
            raise TimestampRecoveryError("Original constraint chunk hash or identity does not match")
        for value in chunk["rows"]:
            if not isinstance(value, dict):
                raise TimestampRecoveryError("Original constraint row must be an object")
            key = (int(value["symbol_id"]), date.fromisoformat(value["trade_date"]))
            if key in constraints or key not in originals:
                raise TimestampRecoveryError("Original constraint row is duplicated or absent from the file")
            if originals[key]["execution"] is None or imports._execution_values(value) != originals[key]["execution"]:
                raise TimestampRecoveryError("Original limits do not match the original file")
            constraints[key] = value
    if len(constraints) != receipt.get("execution_constraint_rows"):
        raise TimestampRecoveryError("Original constraint row count does not match")
    rows = _read(db, select(SecurityStatusDaily).where(SecurityStatusDaily.as_of_batch_id == batch_id)
                 .order_by(SecurityStatusDaily.id), lock)
    if {(row.symbol_id, row.trade_date) for row in rows} != set(constraints) or len(rows) != len(constraints):
        raise TimestampRecoveryError("Batch rows are missing, duplicated or lack original status fingerprints")
    changes = []
    for row in rows:
        key = (row.symbol_id, row.trade_date)
        original = originals[key]
        reconstructed = SecurityStatusDaily(**{field: original[field] for field in ("symbol_id", "trade_date", *imports.VALUE_COLUMNS)},
            status_source=f"historical_csv:{payload.source_name}", source_updated_at=expected, as_of_batch_id=batch_id)
        if (imports._row_values(row) != imports._row_values(original)
                or row.status_source != reconstructed.status_source
                or imports._status_fingerprint(reconstructed) != constraints[key].get("status_fingerprint")):
            raise TimestampRecoveryError("Status fields or original status fingerprint do not match")
        truncated = expected.replace(microsecond=0)
        rounded = truncated + timedelta(seconds=int(expected.microsecond >= 500000))
        if row.source_updated_at not in {expected, truncated, rounded}:
            raise TimestampRecoveryError("Stored timestamp is not an exact legacy truncation/rounding of the original")
        if row.source_updated_at != expected:
            changes.append({"row_id": row.id, "symbol_id": row.symbol_id, "trade_date": row.trade_date.isoformat(),
                "before": row.source_updated_at.isoformat(timespec="microseconds"),
                "after": expected.isoformat(timespec="microseconds"),
                "original_status_fingerprint": constraints[key]["status_fingerprint"]})
    evidence = {"content_sha256": digest, "receipt": _snapshot(receipts[0]),
                "chunks": [_snapshot(item) for item in chunks], "rows": [_snapshot(row) for row in rows]}
    plan = {"can_apply": True, "batch_id": batch_id, "content_sha256": digest,
            "original_receipt_id": receipts[0].id, "verified_rows": len(rows),
            "repair_count": len(changes), "changes": changes, "errors": [],
            "preview_sha256": _hash(evidence)}
    return plan, rows


def preview_status_timestamp_recovery(db: Session, payload: SecurityStatusFileRequest, *, expected_content_sha256: str) -> dict:
    """Read-only; no guessed timestamps or writes even when evidence is rejected."""
    with db.no_autoflush:
        try:
            return _build_plan(db, payload, expected_content_sha256, lock=False)[0]
        except (TimestampRecoveryError, ValueError, KeyError, TypeError, csv.Error) as exc:
            return {"can_apply": False, "repair_count": 0, "changes": [], "errors": [str(exc)], "preview_sha256": None}


def apply_status_timestamp_recovery(db: Session, payload: SecurityStatusFileRequest, *, expected_content_sha256: str,
                                    preview_sha256: str, operator_id: str, confirmed: bool = False) -> dict:
    """Recheck and lock all evidence; caller commits the atomic transaction."""
    if confirmed is not True or not operator_id.strip() or operator_id.strip() in {"system", "cron_worker"}:
        raise TimestampRecoveryError("Explicit personal confirmation and operator identity are required")
    if db.new or db.dirty or db.deleted:
        raise TimestampRecoveryError("Recovery requires a clean session with no unrelated pending writes")
    if db.bind.dialect.name == "mysql":
        engines = dict(db.execute(text("SELECT TABLE_NAME, ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() "
                                      "AND table_name IN ('security_status_daily','data_governance_audit_events')")).all())
        if engines != {"security_status_daily": "InnoDB", "data_governance_audit_events": "InnoDB"}:
            raise TimestampRecoveryError("Timestamp recovery requires InnoDB status and audit tables; nontransactional storage cannot guarantee atomic recovery")
        precision = db.scalar(text("SELECT DATETIME_PRECISION FROM information_schema.columns WHERE table_schema=DATABASE() "
                                   "AND table_name='security_status_daily' AND column_name='source_updated_at'"))
        if precision != 6:
            raise TimestampRecoveryError("Apply migration 0082 (DATETIME(6)) before timestamp recovery")
    with db.begin_nested():
        plan, rows = _build_plan(db, payload, expected_content_sha256, lock=True)
        if plan["preview_sha256"] != preview_sha256:
            raise TimestampRecoveryError("Recovery evidence changed; preview again before applying")
        expected = payload.source_updated_at.astimezone(timezone.utc).replace(tzinfo=None)
        changed_ids = {item["row_id"] for item in plan["changes"]}
        for row in rows:
            if row.id in changed_ids:
                row.source_updated_at = expected
        db.flush()
        db.expire_all()
        # Actual DB round trip, not just an ORM object's in-memory timestamp.
        verified, _ = _build_plan(db, payload, expected_content_sha256, lock=True)
        if verified["repair_count"]:
            raise TimestampRecoveryError("Database did not preserve recovered timestamp precision")
        audit_ids = []
        # Bounded append-only audits; existing import receipts/chunks stay intact.
        for offset in range(0, len(plan["changes"]), 60):
            change_slice = plan["changes"][offset:offset + 60]
            audit = write_audit_event(db, "DATA_BLOCK_RESOLUTION", operator_id=operator_id.strip(),
                business_key=f"status_precision:{plan['batch_id']}:{offset // 60}",
                before={"changes": [{"row_id": item["row_id"], "source_updated_at": item["before"]} for item in change_slice]},
                after={"kind": "security_status_timestamp_recovery", "batch_id": plan["batch_id"],
                       "content_sha256": expected_content_sha256, "original_receipt_id": plan["original_receipt_id"],
                       "preview_sha256": preview_sha256, "changes": change_slice},
                attributes={"confirmed_original_evidence": True, "changed_columns": ["source_updated_at"]},
                note="Restore exact source timestamp from original request, receipt and status fingerprints; preserve all historical rows.")
            audit_ids.append(audit.event_id)
        db.info.pop("security_status_constraints_cache", None)
        db.info["security_status_constraints_epoch"] = db.info.get("security_status_constraints_epoch", 0) + 1
        return {"batch_id": plan["batch_id"], "repaired": len(changed_ids), "audit_ids": audit_ids,
                "idempotent": not changed_ids}
