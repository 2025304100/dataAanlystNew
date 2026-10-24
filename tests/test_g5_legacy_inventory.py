"""HF-07 inventory is read-only and never guesses report adoption."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.g5_dual_run import G5DualRunReport
from app.models.g5_admission_governance import G5AdmissionControl, G5AdmissionEvent  # metadata fixture
from app.models.portfolio import Portfolio
from app.services import g5_legacy_inventory as service
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event


pytestmark = pytest.mark.whitebox


@pytest.fixture
def inventory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([Portfolio(id=1, name="member scope", account_type="simulated", total_capital=1000,
            investable_ratio=.95, cash_reserve_ratio=.05, auto_trade_source_mode="members_only"),
            Portfolio(id=2, name="legacy scope", account_type="simulated", total_capital=1000,
            investable_ratio=.95, cash_reserve_ratio=.05, auto_trade_source_mode="legacy_scan")])
        db.commit()
        yield db
    engine.dispose()


def add_report(db, *, report_id=1, portfolio_id=1, raw=None, bad_hash=False):
    payload = {"portfolio_id": portfolio_id, "status": "COMPLETED", "start_date": "2026-08-03",
        "end_date": "2026-08-14", "total_days": 10, "days_replayed": 10,
        "skipped_days": [], "total_p0_unexplained": 0, "total_p1_hold_noaction_flip": 0,
        "g5_eligible_for_g6": True, "daily_reports": [],
        "provenance": {"capture_mode": "unit_test_validator_contract"}}
    rendered = service._canonical(payload) if raw is None else raw
    original = hashlib.sha256(rendered.encode()).hexdigest()
    record = G5DualRunReport(id=report_id, portfolio_id=portfolio_id, start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 14), total_days=10, days_replayed=10, skipped_day_count=0,
        p0_unexplained_count=0, p1_hold_noaction_flip_count=0, eligible_for_g6=1,
        report_hash="f" * 64 if bad_hash else original, report_json=rendered,
        created_at=datetime(2026, 8, 15))
    db.add(record); db.commit()
    return record


def trusted_validator(monkeypatch):
    """Isolate inventory relationships from the real execution-source validator."""
    monkeypatch.setattr(service, "validate_g5_report", lambda db, record, **kwargs: {
        "status": "COMPLETED", "evidence_verified": True,
        "g5_eligible_for_g6": True, "g5_validation_errors": [], "storage_errors": []})


def audit(db, action="G6_ROLLOUT_STARTED", *, portfolio_id=1, before=None, after=None, attrs=None):
    result = write_audit_event(db, action, portfolio_id=portfolio_id,
        before=before, after=after, attributes=attrs, occurred_at=datetime(2026, 8, 16), operator_id="reviewer")
    db.commit()
    return result.event_id


def test_invalid_originals_keep_typed_id_hash_and_reasons_without_becoming_an_empty_inventory(inventory_db):
    db = inventory_db
    record = add_report(db, raw="{damaged original")
    result = service.build_g5_legacy_inventory(db)
    report = result["reports"][0]
    assert result["report_count"] == result["invalid_report_count"] == 1
    assert report["report_id"] == record.id and report["original_hash"] == record.report_hash
    assert report["parse_status"] == "INVALID_JSON" and report["original_hash_matches"] is None
    assert report["evidence_verified"] is False and report["eligible"] is False
    assert "REPORT_JSON_INVALID" in report["validation_errors"]
    assert report["review_status"] == "requires_review"
    assert result["rollback_material"]["original_reports"][0]["raw_payload_sha256"] == hashlib.sha256(record.report_json.encode()).hexdigest()
    assert db.get(G5DualRunReport, 1).report_json == "{damaged original"


def test_current_member_mode_is_unproven_and_does_not_attach_a_report_reference(inventory_db, monkeypatch):
    trusted_validator(monkeypatch)
    add_report(inventory_db)
    result = service.build_g5_legacy_inventory(inventory_db)
    report = result["reports"][0]
    assert report["references"] == [] and report["historically_adopted"] is False
    assert report["current_source_mode"] == "members_only"
    unproven = result["unresolved_references"][0]
    assert unproven["relation"] == "unproven" and unproven["report_id"] is None
    assert unproven["reason_code"] == "CURRENT_MODE_HAS_NO_PROVEN_G5_REFERENCE"
    assert result["affected_portfolios"][0]["historically_adopted_report_ids"] == []


def test_explicit_real_g6_start_resolves_once_with_all_paths_and_historical_adoption(inventory_db, monkeypatch):
    trusted_validator(monkeypatch)
    record = add_report(inventory_db)
    aid = audit(inventory_db, before={"auto_trade_source_mode": "legacy_scan", "g5_report_id": record.id},
        after={"auto_trade_source_mode": "members_only", "g5_report_id": record.id},
        attrs={"g5_report_id": record.id, "audit_report_hash": record.report_hash})
    result = service.build_g5_legacy_inventory(inventory_db)
    report = result["reports"][0]
    assert report["historically_adopted"] is True and report["eligible"] is True
    assert len(report["references"]) == 1 and report["references"][0]["audit_id"] == aid
    assert report["references"][0]["reference_paths"] == ["after", "attributes", "before"]
    assert all(ref["relation"] == "explicit" and ref["reference_valid"] for ref in report["references"])
    assert result["affected_portfolios"][0]["historically_adopted_report_ids"] == [record.id]
    assert result["unresolved_references"] == []
    assert result["reviewable"] and result["rollback_material"]["trading_facts_modified"] is False


def test_source_switch_fields_are_explicit_but_reconciliation_report_ids_are_not_g5(inventory_db, monkeypatch):
    trusted_validator(monkeypatch)
    record = add_report(inventory_db)
    audit(inventory_db, "DATA_BLOCK_RESOLUTION", before={"auto_trade_source_mode": "legacy_scan"},
        after={"auto_trade_source_mode": "members_only"},
        attrs={"confirmation_type": "AUTO_TRADE_SOURCE_SWITCH", "report_id": record.id})
    ignored = audit(inventory_db, "DATA_BLOCK_RESOLUTION", attrs={
        "confirmation_type": "RECONCILIATION_CONFIRMED", "report_id": record.id})
    audit(inventory_db, "DATA_BLOCK_RESOLUTION", attrs={
        "confirmation_type": "AUTO_TRADE_SOURCE_SWITCH", "report_id": "reconciliation::1::2026-08-01"})
    result = service.build_g5_legacy_inventory(inventory_db)
    refs = result["reports"][0]["references"]
    assert len(refs) == 1 and refs[0]["historical_adoption"] is True
    assert ignored not in {ref["audit_id"] for ref in result["rollback_material"]["audit_references"]}
    assert any(ref["reason_code"] == "G5_REFERENCE_NOT_RECORDED" for ref in result["unresolved_references"])


def test_rollback_without_report_id_preserves_unknown_instead_of_attributing_latest_report(inventory_db, monkeypatch):
    trusted_validator(monkeypatch)
    add_report(inventory_db)
    aid = audit(inventory_db, "G6_ROLLOUT_ROLLED_BACK", before={"auto_trade_source_mode": "members_only"},
        after={"auto_trade_source_mode": "legacy_scan"}, attrs={"reason": "operator_requested"})
    result = service.build_g5_legacy_inventory(inventory_db)
    assert result["reports"][0]["references"] == []
    assert any(ref["audit_id"] == aid and ref["relation"] == "unproven" for ref in result["unresolved_references"])


def test_explicit_hash_only_reference_resolves_and_conflicting_id_hash_stays_unresolved(inventory_db, monkeypatch):
    trusted_validator(monkeypatch)
    record = add_report(inventory_db)
    audit(inventory_db, attrs={"audit_report_hash": record.report_hash})
    bad_aid = audit(inventory_db, before={"g5_report_id": record.id, "auto_trade_source_mode": "legacy_scan"},
        after={"g5_report_id": record.id, "auto_trade_source_mode": "members_only"},
        attrs={"g5_report_id": record.id, "audit_report_hash": "a" * 64})
    result = service.build_g5_legacy_inventory(inventory_db)
    report = result["reports"][0]
    assert any(ref["report_id"] == record.id and ref["reference_valid"] for ref in report["references"])
    assert any(ref["audit_id"] == bad_aid and ref["reason_code"] == "G5_REFERENCE_HASH_MISMATCH" for ref in result["unresolved_references"])
    assert report["review_status"] == "requires_review"
    assert all(not ref["historical_adoption"] for ref in report["references"] if ref["audit_id"] == bad_aid)


def test_portfolio_filter_does_not_sweep_other_reports_or_audits_and_cross_owner_is_not_adoption(inventory_db, monkeypatch):
    trusted_validator(monkeypatch)
    one = add_report(inventory_db)
    two = add_report(inventory_db, report_id=2, portfolio_id=2)
    audit(inventory_db, portfolio_id=2, attrs={"g5_report_id": one.id})
    other_aid = audit(inventory_db, portfolio_id=2, attrs={"g5_report_id": two.id})
    filtered = service.build_g5_legacy_inventory(inventory_db, portfolio_id=1)
    assert [report["report_id"] for report in filtered["reports"]] == [one.id]
    assert filtered["reports"][0]["references"] == []
    assert other_aid not in {ref["audit_id"] for ref in filtered["rollback_material"]["audit_references"]}
    all_result = service.build_g5_legacy_inventory(inventory_db)
    assert any(ref.get("reason_code") == "G5_REFERENCE_PORTFOLIO_MISMATCH" for ref in all_result["unresolved_references"])
    assert all_result["reports"][0]["historically_adopted"] is False


def test_no_autoflush_no_writes_and_stable_snapshot_uses_persisted_columns(inventory_db, monkeypatch):
    trusted_validator(monkeypatch)
    db = inventory_db
    record = add_report(db)
    baseline = service.build_g5_legacy_inventory(db)
    record.report_json = '{"dirty_uncommitted_edit":true}'
    pending = Portfolio(name="pending must remain pending", account_type="simulated", total_capital=1,
        investable_ratio=1, cash_reserve_ratio=0)
    db.add(pending)
    statements = []
    def track(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().upper())
    event.listen(db.get_bind(), "before_cursor_execute", track)
    try:
        result = service.build_g5_legacy_inventory(db)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", track)
    assert result["snapshot_id"] == baseline["snapshot_id"]
    assert result["reports"][0]["original_hash_matches"] is True
    assert pending in db.new and pending.id is None and record in db.dirty
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE")) for sql in statements)
    assert result["generated_at"] != baseline["generated_at"]


def test_real_validator_exposes_legacy_provenance_and_original_hash_conflicts(inventory_db):
    add_report(inventory_db, bad_hash=True)
    result = service.build_g5_legacy_inventory(inventory_db)
    report = result["reports"][0]
    assert report["original_hash_matches"] is False
    assert not report["eligible"] and not report["evidence_verified"]
    assert "ORIGINAL_HASH_MISMATCH" in report["validation_errors"]
    assert len(report["validation_errors"]) > 1


def test_storage_missing_is_not_a_completed_empty_inventory_and_creates_nothing(inventory_db, monkeypatch):
    trusted_validator(monkeypatch)
    db = inventory_db
    add_report(db)
    db.execute(text("DROP TABLE g5_dual_run_reports")); db.commit()
    tables = set(inspect(db.connection()).get_table_names())
    first = service.build_g5_legacy_inventory(db)
    second = service.build_g5_legacy_inventory(db)
    assert not first["reviewable"] and not first["counts_complete"]
    assert first["storage_errors"][0]["source"] == "g5_dual_run_reports"
    assert first["storage_errors"][0]["code"] == "INVENTORY_TABLE_MISSING"
    assert first["snapshot_id"] == second["snapshot_id"]
    assert set(inspect(db.connection()).get_table_names()) == tables


def test_validator_governance_storage_unknown_is_preserved_not_collapsed_to_clean_invalid(inventory_db, monkeypatch):
    add_report(inventory_db)
    monkeypatch.setattr(service, "validate_g5_report", lambda *args, **kwargs: {
        "status": "INVALID", "evidence_verified": False, "g5_eligible_for_g6": False,
        "g5_validation_errors": ["准入隔离表无法读取"], "storage_errors": [{
            "code": "G5_GOVERNANCE_STORAGE_UNAVAILABLE", "detail": "governance schema not installed"}]})
    result = service.build_g5_legacy_inventory(inventory_db)
    assert not result["reviewable"]
    assert result["storage_errors"][0]["code"] == "G5_GOVERNANCE_STORAGE_UNAVAILABLE"
    assert result["storage_errors"][0]["report_id"] == 1
    assert result["report_count"] == result["invalid_report_count"] == 1


def test_validator_database_error_and_unreadable_audit_stay_explicit_unknowns(inventory_db, monkeypatch):
    db = inventory_db
    add_report(db)
    aid = audit(db)
    db.get(DataGovernanceAuditEvent, aid).attributes_json = "{damaged audit"; db.commit()
    def fail(*args, **kwargs):
        raise OperationalError("private sql", {}, Exception("credentials should not be echoed"))
    monkeypatch.setattr(service, "validate_g5_report", fail)
    result = service.build_g5_legacy_inventory(db)
    assert not result["reviewable"] and result["storage_errors"][0]["error_type"] == "OperationalError"
    assert "private sql" not in service._canonical(result) and "credentials" not in service._canonical(result)
    assert any(ref["audit_id"] == aid and ref["unreadable_fields"] == ["attributes"] for ref in result["unresolved_references"])


def test_current_read_locks_actual_sources_and_forwards_validator_flag_without_changing_snapshot(inventory_db, monkeypatch):
    db = inventory_db
    add_report(db)
    flags = []
    def validator(db, record, *, portfolio_id, current_read=False):
        flags.append(current_read)
        return {"status": "COMPLETED", "evidence_verified": True,
                "g5_eligible_for_g6": True, "g5_validation_errors": [], "storage_errors": []}
    monkeypatch.setattr(service, "validate_g5_report", validator)
    preview = service.build_g5_legacy_inventory(db)
    statements = []
    def inspect_orm_reads(state):
        statement = state.statement
        if getattr(statement, "is_select", False):
            statements.append((str(statement), getattr(statement, "_for_update_arg", None) is not None))
    event.listen(db, "do_orm_execute", inspect_orm_reads)
    try:
        current = service.build_g5_legacy_inventory(db, current_read=True)
    finally:
        event.remove(db, "do_orm_execute", inspect_orm_reads)
    assert flags == [False, True]
    assert current["snapshot_id"] == preview["snapshot_id"]
    assert len(statements) == 3 and all(locked for _, locked in statements)


def test_missing_governance_is_unknown_even_when_there_are_no_reports_to_validate(inventory_db):
    db = inventory_db
    db.execute(text("DROP TABLE g5_admission_events")); db.commit()
    before = set(inspect(db.connection()).get_table_names())
    inventory = service.build_g5_legacy_inventory(db)
    assert inventory["report_count"] == 0 and not inventory["reviewable"]
    assert inventory["storage_errors"][0]["code"] == "G5_GOVERNANCE_STORAGE_UNAVAILABLE"
    assert set(inspect(db.connection()).get_table_names()) == before
