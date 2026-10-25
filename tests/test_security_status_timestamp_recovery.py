"""Recovery is evidence-based and atomic through the public recovery seam."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json

import pytest
from sqlalchemy import select, func, text

from app.models.security_status import SecurityStatusDaily
from app.models.symbol import Symbol
from app.schemas.security_status import SecurityStatusFileRequest
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.security_status.local_import import (
    get_imported_execution_constraints, import_file, preview_import,
)

pytestmark = pytest.mark.whitebox


def original_import(db, *, source_time="2025-09-02T10:00:00.126864+00:00"):
    symbol = Symbol(symbol="600000", name="recovery", asset_type="stock", market="SH")
    db.add(symbol)
    db.commit()
    payload = SecurityStatusFileRequest(
        csv_text="symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n"
                 "600000,2025-09-01,0,0,0,1,1999-11-10,,LIMITED,11,9,0.01\n",
        source_name="provider_archive", source_reference="archive://daily/20250901",
        source_version="release-20250901", source_updated_at=source_time,
    )
    preview = preview_import(db, payload)
    receipt = import_file(db, payload, preview_sha256=preview["content_sha256"])
    db.commit()
    row = db.scalar(select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id == symbol.id))
    row.source_updated_at = row.source_updated_at.replace(microsecond=0)
    db.commit()
    return symbol.id, row.id, payload, receipt


def test_original_evidence_restores_microseconds_without_replacing_history(db_session):
    from app.services.security_status.timestamp_recovery import (
        preview_status_timestamp_recovery, apply_status_timestamp_recovery,
    )
    sid, row_id, payload, receipt = original_import(db_session)
    assert get_imported_execution_constraints(db_session, sid, date(2025, 9, 1)) is None
    count_before = db_session.scalar(select(func.count()).select_from(DataGovernanceAuditEvent))
    plan = preview_status_timestamp_recovery(db_session, payload, expected_content_sha256=receipt["content_sha256"])
    assert plan["can_apply"] is True
    assert plan["repair_count"] == 1
    assert db_session.get(SecurityStatusDaily, row_id).source_updated_at.microsecond == 0
    result = apply_status_timestamp_recovery(db_session, payload,
        expected_content_sha256=receipt["content_sha256"], preview_sha256=plan["preview_sha256"],
        operator_id="review-user", confirmed=True)
    db_session.commit()
    db_session.expire_all()
    assert result["repaired"] == 1
    constraint = get_imported_execution_constraints(db_session, sid, date(2025, 9, 1))
    assert constraint["upper_limit_price"] == 11
    row = db_session.get(SecurityStatusDaily, row_id)
    assert row.source_updated_at == datetime(2025, 9, 2, 10, 0, 0, 126864)
    assert row.as_of_batch_id == receipt["batch_id"]
    assert db_session.scalar(select(func.count()).select_from(SecurityStatusDaily)) == 1
    assert db_session.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == count_before + 1


def recover(db, payload, receipt, *, plan=None):
    from app.services.security_status.timestamp_recovery import preview_status_timestamp_recovery, apply_status_timestamp_recovery
    plan = plan or preview_status_timestamp_recovery(db, payload, expected_content_sha256=receipt["content_sha256"])
    return apply_status_timestamp_recovery(db, payload, expected_content_sha256=receipt["content_sha256"],
        preview_sha256=plan["preview_sha256"], operator_id="review-user", confirmed=True)


def test_rounded_midnight_restores_original_day_and_repeat_is_noop(db_session):
    sid, row_id, payload, receipt = original_import(db_session, source_time="2025-09-02T23:59:59.999999+00:00")
    row = db_session.get(SecurityStatusDaily, row_id)
    row.source_updated_at = datetime(2025, 9, 3)
    db_session.commit()
    audits_before = {a.id: a.after_json for a in db_session.scalars(select(DataGovernanceAuditEvent))}
    assert recover(db_session, payload, receipt)["repaired"] == 1
    db_session.commit()
    assert db_session.get(SecurityStatusDaily, row_id).source_updated_at == datetime(2025, 9, 2, 23, 59, 59, 999999)
    assert recover(db_session, payload, receipt)["idempotent"] is True
    db_session.commit()
    assert {a.id: a.after_json for a in db_session.scalars(select(DataGovernanceAuditEvent)) if a.id in audits_before} == audits_before
    assert get_imported_execution_constraints(db_session, sid, date(2025, 9, 1))["batch_id"] == receipt["batch_id"]


@pytest.mark.parametrize("tamper", ["file", "digest", "receipt_time", "receipt_source", "receipt_json_type", "chunk_hash", "state", "timestamp", "missing_row", "duplicate_receipt", "duplicate_chunk"])
def test_ambiguous_or_changed_evidence_never_changes_rows(db_session, tamper):
    from app.services.security_status.timestamp_recovery import preview_status_timestamp_recovery
    _, row_id, payload, receipt = original_import(db_session)
    digest = receipt["content_sha256"]
    audit = db_session.scalar(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.business_key == receipt["batch_id"]))
    chunk = db_session.scalar(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.business_key == receipt["batch_id"] + ":execution:0"))
    if tamper == "file":
        payload = payload.model_copy(update={"csv_text": payload.csv_text.replace(",11,9,", ",12,9,")})
    elif tamper == "digest":
        digest = "a" * 64
    elif tamper.startswith("receipt_"):
        value = json.loads(audit.after_json)
        if tamper == "receipt_time":
            value["source_updated_at"] = "2025-09-02T10:00:00.654321+00:00"
        elif tamper == "receipt_source":
            value["source_reference"] = "archive://forged"
        else:
            value = []
        audit.after_json = json.dumps(value)
    elif tamper == "chunk_hash":
        chunk.after_json = chunk.after_json.replace('"upper_limit_price":11.0', '"upper_limit_price":12.0')
    elif tamper == "state":
        db_session.get(SecurityStatusDaily, row_id).is_st = True
    elif tamper == "timestamp":
        db_session.get(SecurityStatusDaily, row_id).source_updated_at = datetime(2025, 9, 2, 10, 0, 1, 123)
    elif tamper == "missing_row":
        db_session.delete(db_session.get(SecurityStatusDaily, row_id))
    elif tamper.startswith("duplicate_"):
        source = audit if tamper == "duplicate_receipt" else chunk
        db_session.add(DataGovernanceAuditEvent(**{col.name: getattr(source, col.name) for col in source.__table__.columns if col.name != "id"}))
    db_session.commit()
    before = list(db_session.execute(select(SecurityStatusDaily.__table__)))
    audit_count = db_session.scalar(select(func.count()).select_from(DataGovernanceAuditEvent))
    plan = preview_status_timestamp_recovery(db_session, payload, expected_content_sha256=digest)
    assert plan["can_apply"] is False
    assert plan["errors"]
    assert list(db_session.execute(select(SecurityStatusDaily.__table__))) == before
    assert db_session.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == audit_count


def test_stale_preview_and_unconfirmed_apply_do_not_write(db_session):
    from app.services.security_status.timestamp_recovery import preview_status_timestamp_recovery, apply_status_timestamp_recovery, TimestampRecoveryError
    _, row_id, payload, receipt = original_import(db_session)
    plan = preview_status_timestamp_recovery(db_session, payload, expected_content_sha256=receipt["content_sha256"])
    with pytest.raises(TimestampRecoveryError, match="confirmation"):
        apply_status_timestamp_recovery(db_session, payload, expected_content_sha256=receipt["content_sha256"],
            preview_sha256=plan["preview_sha256"], operator_id="review-user")
    audit = db_session.scalar(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.business_key == receipt["batch_id"]))
    audit.note = "evidence modified after preview"
    db_session.commit()
    with pytest.raises(TimestampRecoveryError, match="evidence changed"):
        recover(db_session, payload, receipt, plan=plan)
    assert db_session.get(SecurityStatusDaily, row_id).source_updated_at.microsecond == 0


def test_audit_write_failure_rolls_back_recovered_timestamp(db_session):
    _, row_id, payload, receipt = original_import(db_session)
    db_session.execute(text("CREATE TRIGGER fail_precision_audit BEFORE INSERT ON data_governance_audit_events "
                            "WHEN NEW.business_key LIKE 'status_precision:%' BEGIN SELECT RAISE(ABORT, 'review audit failure'); END"))
    db_session.commit()
    with pytest.raises(Exception, match="review audit failure"):
        recover(db_session, payload, receipt)
    db_session.expire_all()
    assert db_session.get(SecurityStatusDaily, row_id).source_updated_at.microsecond == 0
    assert db_session.scalar(select(func.count()).select_from(SecurityStatusDaily)) == 1
    assert db_session.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == 2


def test_0082_refuses_lossy_downgrade(db_session):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / "alembic/versions/2026_10_25_0082_security_status_datetime6.py"
    spec = importlib.util.spec_from_file_location("recovery_migration_0082", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        with pytest.raises(RuntimeError, match="[Pp]reserve|[Ll]oss|precision"):
            migration.downgrade()
