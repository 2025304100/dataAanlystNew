"""HF-07 controls run only in isolated databases; never inventory production."""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.models.g5_admission_governance import G5AdmissionControl, G5AdmissionEvent
from app.models.g5_dual_run import G5DualRunReport
from app.models.portfolio import Portfolio
from app.services import g5_admission_governance as service
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services.g5_legacy_inventory import build_g5_legacy_inventory

pytestmark = pytest.mark.whitebox
NOTE = "逐项核对遗留报告来源及已有组合影响，只控制新的准入"


@pytest.fixture
def governance_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([Portfolio(id=i, name=f"scope-{i}", account_type="simulated", total_capital=1000,
            investable_ratio=.95, cash_reserve_ratio=.05, auto_trade_source_mode="legacy_scan") for i in (1, 2)])
        db.commit()
        yield db
    engine.dispose()


def call_control(db, *, frozen=True, portfolio_id=None, **changes):
    args = dict(frozen=frozen, portfolio_id=portfolio_id,
        expected_version=service.get_g5_admission_state(db, portfolio_id)["version"],
        review_note=NOTE, request_key=f"control-{portfolio_id}-{frozen}-{service.get_g5_admission_state(db, portfolio_id)['version']}",
        operator_id="alice", acknowledge_only_new_admission=True)
    if not frozen:
        inventory = build_g5_legacy_inventory(db, portfolio_id=portfolio_id)
        args["inventory_snapshot_id"] = inventory["snapshot_id"]
        args["impact_reviews"] = [dict(portfolio_id=item["portfolio_id"], disposition="retain_existing_runtime",
            rationale=NOTE, evidence_refs=[f"inventory:{inventory['snapshot_id']}"]) for item in inventory["affected_portfolios"]]
    return service.set_g5_admission_freeze(db, **(args | changes))


def add_bad_report(db, *, report_id=1):
    row = G5DualRunReport(id=report_id, portfolio_id=1, start_date=date(2026, 8, 3), end_date=date(2026, 8, 14),
        total_days=10, days_replayed=10, skipped_day_count=0, p0_unexplained_count=0,
        p1_hold_noaction_flip_count=0, eligible_for_g6=1, report_hash="a" * 64, report_json="{damaged legacy original")
    db.add(row); db.commit()
    return row


def invalidate(db, report, *, action="invalidate", **changes):
    args = dict(action=action, expected_report_hash=report.report_hash,
        inventory_snapshot_id=build_g5_legacy_inventory(db, portfolio_id=report.portfolio_id)["snapshot_id"],
        expected_version=service.get_g5_admission_state(db)["version"], review_note=NOTE,
        request_key=f"{action}-original-{report.id}-{service.get_g5_admission_state(db)['version']}",
        operator_id="alice", acknowledge_only_new_admission=True)
    return service.append_g5_report_invalidation(db, report.id, **(args | changes))


def test_initial_read_is_frozen_and_read_only_without_flushing_caller_changes(governance_db):
    db = governance_db
    portfolio = db.get(Portfolio, 1)
    portfolio.name = "unflushed"
    statements = []
    def trace(conn, cursor, statement, *args):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", trace)
    state = service.get_g5_admission_state(db, 1)
    assert state["frozen"] and state["new_admission_only"] and not state["affects_existing_runtime"]
    assert state["storage_status"] == "available" and state["version"] == 0
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
    assert portfolio in db.dirty
    event.remove(db.bind, "before_cursor_execute", trace)


def test_missing_governance_schema_is_explicitly_unavailable_and_never_created():
    engine = create_engine("sqlite://")
    with Session(engine) as db:
        state = service.get_g5_admission_state(db, 1)
        assert state["frozen"] and state["storage_status"] == "unavailable"
        assert state["reason_codes"] == ["G5_GOVERNANCE_STORAGE_UNAVAILABLE"]
        assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_release_requires_inventory_snapshot_and_missing_unknown_is_not_empty_success(governance_db):
    db = governance_db
    with pytest.raises(service.G5GovernanceError, match="盘点") as caught:
        call_control(db, frozen=False, inventory_snapshot_id=None)
    assert caught.value.code == "G5_INVENTORY_CHANGED"
    assert db.scalar(select(func.count()).select_from(G5AdmissionEvent)) == 0
    assert service.get_g5_admission_state(db)["frozen"]


def test_reviewed_empty_inventory_release_freeze_and_append_only_receipts(governance_db):
    db = governance_db
    released = call_control(db, frozen=False)
    assert released["frozen"] is False and released["version"] == 1
    frozen = call_control(db, frozen=True)
    assert frozen["version"] == 2 and service.get_g5_admission_state(db, 1)["frozen"]
    events = list(db.scalars(select(G5AdmissionEvent).order_by(G5AdmissionEvent.id)))
    assert [row.event_type for row in events] == ["RELEASE", "FREEZE"]
    assert events[1].previous_event_id == events[0].id
    assert all(json.loads(row.payload_json)["rollback_material"] for row in events)
    assert db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == 2
    assert db.get(Portfolio, 1).auto_trade_source_mode == "legacy_scan"


def test_idempotent_request_returns_original_receipt_and_different_content_rejected(governance_db):
    db = governance_db
    first = call_control(db, request_key="same-freeze-request", expected_version=0)
    second = call_control(db, request_key="same-freeze-request", expected_version=0)
    assert second["idempotent"] and second["event_id"] == first["event_id"]
    with pytest.raises(service.G5GovernanceError) as caught:
        call_control(db, request_key="same-freeze-request", expected_version=0, review_note=NOTE + "追加修改")
    assert caught.value.code == "G5_REQUEST_KEY_REUSED"
    assert db.scalar(select(func.count()).select_from(G5AdmissionEvent)) == 1


def test_stale_version_and_stale_inventory_never_release(governance_db):
    db = governance_db
    inventory = build_g5_legacy_inventory(db)
    call_control(db)
    with pytest.raises(service.G5GovernanceError) as caught:
        call_control(db, frozen=False, expected_version=0)
    assert caught.value.code == "G5_CONTROL_VERSION_CHANGED"
    add_bad_report(db)
    with pytest.raises(service.G5GovernanceError) as caught:
        call_control(db, frozen=False, inventory_snapshot_id=inventory["snapshot_id"])
    assert caught.value.code == "G5_INVENTORY_CHANGED"
    assert service.get_g5_admission_state(db)["frozen"]


def test_invalid_report_requires_hash_bound_invalidation_before_release(governance_db):
    db = governance_db
    report = add_bad_report(db)
    before = (report.report_hash, report.report_json, db.get(Portfolio, 1).auto_trade_source_mode)
    with pytest.raises(service.G5GovernanceError) as caught:
        call_control(db, frozen=False)
    assert caught.value.code == "G5_INVALID_REPORTS_NOT_ISOLATED"
    result = invalidate(db, report)
    assert result["action"] == "invalidate"
    assert "G5_REPORT_INVALIDATED" in service.report_revocation_reason(db, report)
    released = call_control(db, frozen=False)
    assert released["frozen"] is False
    assert (report.report_hash, report.report_json, db.get(Portfolio, 1).auto_trade_source_mode) == before
    event_row = db.get(G5AdmissionEvent, result["event_id"])
    payload = json.loads(event_row.payload_json)
    assert payload["inventory"]["reports"][0]["raw_payload_sha256"]
    assert payload["rollback_material"]["original_hash"] == before[0]
    assert event_row.audit_event_id is not None


def test_impact_review_required_for_each_affected_portfolio(governance_db):
    db = governance_db
    # A real current source mode is an unresolved dependency even without reports.
    db.get(Portfolio, 1).auto_trade_source_mode = "members_only"; db.commit()
    with pytest.raises(service.G5GovernanceError) as caught:
        call_control(db, frozen=False, impact_reviews=[])
    assert caught.value.code == "G5_IMPACT_REVIEW_REQUIRED" and caught.value.details == [1]
    assert call_control(db, frozen=False)["frozen"] is False


def test_portfolio_release_cannot_override_global_freeze(governance_db):
    db = governance_db
    result = call_control(db, frozen=False, portfolio_id=1)
    assert result["scope_frozen"] is False and result["frozen"] is True
    call_control(db, frozen=False)
    assert not service.get_g5_admission_state(db, 1)["frozen"]
    call_control(db, frozen=True, portfolio_id=1)
    assert service.get_g5_admission_state(db, 1)["frozen"]
    assert not service.get_g5_admission_state(db, 2)["frozen"]


def test_unknown_portfolio_cannot_create_scoped_governance(governance_db):
    with pytest.raises(service.G5GovernanceError) as caught:
        call_control(governance_db, portfolio_id=999)
    assert caught.value.code == "PORTFOLIO_NOT_FOUND"
    assert governance_db.get(G5AdmissionControl, "portfolio:999") is None


@pytest.mark.parametrize("operator", ["system", "cron_worker", "system:worker", ""])
def test_background_actor_cannot_sign_governance_review(governance_db, operator):
    with pytest.raises(service.G5GovernanceError) as caught:
        call_control(governance_db, operator_id=operator)
    assert caught.value.code == "G5_PERSONAL_REVIEW_REQUIRED"


@pytest.mark.parametrize("changes,code", [({"review_note": "        短"}, "G5_REVIEW_NOTE_REQUIRED"),
    ({"acknowledge_only_new_admission": False}, "G5_NEW_ADMISSION_ACK_REQUIRED"),
    ({"request_key": "short"}, "G5_REQUEST_KEY_REQUIRED")])
def test_review_requires_meaningful_note_ack_and_key(governance_db, changes, code):
    with pytest.raises(service.G5GovernanceError) as caught:
        call_control(governance_db, **changes)
    assert caught.value.code == code


def test_invalidating_requires_frozen_window_current_hash_and_snapshot(governance_db):
    db = governance_db
    call_control(db, frozen=False)
    report = add_bad_report(db)
    with pytest.raises(service.G5GovernanceError) as caught:
        invalidate(db, report)
    assert caught.value.code == "G5_FREEZE_REQUIRED"
    call_control(db)
    with pytest.raises(service.G5GovernanceError) as caught:
        invalidate(db, report, expected_report_hash="b" * 64)
    assert caught.value.code == "G5_REPORT_IDENTITY_CHANGED"
    with pytest.raises(service.G5GovernanceError) as caught:
        invalidate(db, report, inventory_snapshot_id="b" * 64)
    assert caught.value.code == "G5_INVENTORY_CHANGED"


def test_restore_cannot_turn_still_invalid_original_into_admission_evidence(governance_db):
    db = governance_db
    report = add_bad_report(db)
    invalidate(db, report)
    with pytest.raises(service.G5GovernanceError) as caught:
        invalidate(db, report, action="restore")
    assert caught.value.code == "G5_REPORT_STILL_INVALID"
    assert "G5_REPORT_INVALIDATED" in service.report_revocation_reason(db, report)


def test_reviewed_restore_appends_and_keeps_global_freeze(governance_db, monkeypatch):
    db = governance_db
    report = add_bad_report(db)
    invalid = invalidate(db, report)
    import app.services.g5_dual_run_audit as audit
    calls = []
    def proven(db, row, **kwargs):
        calls.append(kwargs)
        return {"evidence_verified": True, "g5_eligible_for_g6": True, "g5_validation_errors": []}
    monkeypatch.setattr(audit, "validate_g5_report", proven)
    restored = invalidate(db, report, action="restore")
    assert restored["event_id"] != invalid["event_id"]
    assert calls[-1]["current_read"] is True and calls[-1]["check_invalidation"] is False
    assert service.report_revocation_reason(db, report) is None
    assert service.get_g5_admission_state(db)["frozen"]
    assert db.get(G5AdmissionEvent, invalid["event_id"]).event_type == "INVALIDATE_REPORT"


def test_event_update_and_delete_are_rejected(governance_db):
    db = governance_db
    result = call_control(db); db.commit()
    row = db.get(G5AdmissionEvent, result["event_id"])
    row.review_note = "attempt overwrite"
    with pytest.raises(ValueError, match="immutable"):
        db.flush()
    db.rollback()
    db.delete(db.get(G5AdmissionEvent, result["event_id"]))
    with pytest.raises(ValueError, match="immutable"):
        db.flush()
    db.rollback()
    assert db.get(G5AdmissionEvent, result["event_id"]).review_note == NOTE


def test_audit_failure_rolls_back_entire_control_change(governance_db, monkeypatch):
    db = governance_db
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(service, "write_audit_event", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        call_control(db, frozen=False)
    assert service.get_g5_admission_state(db)["frozen"]
    assert db.scalar(select(func.count()).select_from(G5AdmissionEvent)) == 0
    assert db.scalar(select(func.count()).select_from(G5AdmissionControl)) == 0


def test_mismatched_control_head_fails_closed(governance_db):
    db = governance_db
    call_control(db, frozen=False)
    db.get(G5AdmissionControl, "global").last_event_id = None; db.flush()
    result = service.get_g5_admission_state(db)
    assert result["frozen"] and "G5_CONTROL_INCONSISTENT" in result["reasons"][0]


def test_three_new_admission_consumers_share_freeze_and_rollback_is_still_possible(governance_db):
    db = governance_db
    from app.services.g6_graduated_rollout import evaluate_g6_readiness, rollback_g6_rollout, start_g6_rollout
    from app.services.g7_operational_readiness import evaluate_g7_operational_status
    from app.services.auto_trade_dual_run import can_switch_to_member_source
    assert not evaluate_g6_readiness(db, portfolio_id=1).eligible
    assert any("G5_NEW_ADMISSION_FROZEN" in reason for reason in evaluate_g6_readiness(db, portfolio_id=1).blockers)
    g7 = evaluate_g7_operational_status(db, portfolio_id=1)
    assert not g7.ready_for_expansion and any("G5_NEW_ADMISSION_FROZEN" in reason for reason in g7.operational_blockers)
    can_switch, reason = can_switch_to_member_source(db, portfolio_id=1)
    assert not can_switch and "G5_NEW_ADMISSION_FROZEN" in reason
    with pytest.raises(service.G5GovernanceError):
        start_g6_rollout(db, portfolio_id=1)
    p = db.get(Portfolio, 1); p.auto_trade_source_mode = "members_only"; db.flush()
    assert rollback_g6_rollout(db, portfolio_id=1, operator_id="alice", reason=NOTE).status == "ROLLED_BACK"
    assert p.auto_trade_source_mode == "legacy_scan"


def test_http_global_permission_separates_inventory_read_from_review(governance_db):
    from app.api.routes.g5_admission_governance import router
    app = FastAPI(); app.include_router(router)
    def session():
        yield governance_db
    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        assert client.get("/g5-admission/inventory", params={"X-User": "alice", "X-Role": "owner"}).status_code == 403
        inventory = client.get("/g5-admission/inventory", params={"X-User": "auditor", "X-Role": "auditor"})
        assert inventory.status_code == 200 and inventory.json()["report_count"] == 0
        payload = dict(frozen=False, expected_version=0, inventory_snapshot_id=inventory.json()["snapshot_id"],
            review_note=NOTE, request_key="http-review-release", acknowledge_only_new_admission=True)
        assert client.post("/g5-admission/control", params={"X-User": "auditor", "X-Role": "auditor"}, json=payload).status_code == 403
        assert client.post("/g5-admission/control", params={"X-User": "owner", "X-Role": "owner"},
            json=payload | {"portfolio_id": 1}).status_code == 403
        result = client.post("/g5-admission/control", params={"X-User": "admin", "X-Role": "admin"}, json=payload)
        assert result.status_code == 200 and not result.json()["frozen"]
        assert client.get("/g5-admission/control", params={"X-User": "auditor", "X-Role": "auditor"}).json()["frozen"] is False


def test_additive_migration_in_isolated_db_preserves_original_and_defaults_frozen(governance_db):
    """Exercise only 0075 against this test's in-memory engine, never live DB."""
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    db = governance_db
    record = add_bad_report(db)
    original = (record.report_json, record.report_hash, db.get(Portfolio, 1).auto_trade_source_mode)
    db.close()
    G5AdmissionEvent.__table__.drop(db.bind)
    G5AdmissionControl.__table__.drop(db.bind)
    filename = Path(__file__).resolve().parents[1] / "alembic/versions/2026_10_23_0075_wps_0023_075_g5_admission_governance.py"
    spec = importlib.util.spec_from_file_location("isolated_g5_migration", filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with db.bind.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
    assert service.get_g5_admission_state(db)["frozen"]
    assert db.scalar(select(func.count()).select_from(G5AdmissionEvent)) == 0
    current = db.get(G5DualRunReport, record.id)
    assert (current.report_json, current.report_hash, db.get(Portfolio, 1).auto_trade_source_mode) == original
    with pytest.raises(RuntimeError, match="preserved"):
        module.downgrade()
    assert service.TABLES <= set(inspect(db.bind).get_table_names())
