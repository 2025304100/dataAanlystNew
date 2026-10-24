"""Real audit history contracts: indexes, batches, read-only details and errors."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import portfolio_inspection_history as routes
from app.db.base import Base
from app.db.session import get_db
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.schemas.portfolio_inspection_history import HistoryFilters
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services.portfolio_inspection_history import (
    get_inspection_history_batch, query_inspection_history,
    search_inspection_history_securities,
)
from app.services.portfolio_inspection_evidence import get_inspection_history_material, export_inspection_history_evidence

pytestmark = pytest.mark.whitebox


@pytest.fixture
def store():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Portfolio.__table__, Symbol.__table__, DataGovernanceAuditEvent.__table__])
    with Session(engine) as db:
        for pid in (1, 2):
            db.add(Portfolio(id=pid, name=f"history-{pid}", account_type="simulation", total_capital=10000,
                             investable_ratio=.95, cash_reserve_ratio=.05))
        db.add_all([
            Symbol(id=1, symbol="000001", name="平安银行", market="CN", asset_type="stock"),
            Symbol(id=2, symbol="600000", name="浦发银行", market="CN", asset_type="stock"),
        ])
        db.commit()
        yield db
    engine.dispose()


def event(db, *, pid=1, action="DATA_BLOCK_RESOLUTION", attrs=None, corr=None,
          when=datetime(2026, 10, 22, 10, 30), note=None, symbol_id=None):
    result = write_audit_event(db, action, portfolio_id=pid, symbol_id=symbol_id,
                               attributes=attrs, correlation_id=corr, occurred_at=when,
                               operator_id="reviewer-1", note=note)
    db.commit()
    return result.event_id


def filters(**kwargs):
    return HistoryFilters(occurred_from=datetime(2000, 1, 1), occurred_to=datetime(2099, 1, 1), **kwargs)


def test_security_or_and_trade_date_must_match_same_object_and_preserve_full_batch(store):
    first = event(store, attrs={"batch_id": "batch-1", "review_decision": "approved", "objects": [
        {"security_id": "CN:000001", "trade_date": "2026-10-19", "result": "success"},
        {"security_id": "CN:600000", "trade_date": "2026-10-20", "result": "failed"},
    ]})
    wrong = query_inspection_history(store, 1, filters(security_ids=["CN:000001"],
                trade_date_from=date(2026, 10, 20), trade_date_to=date(2026, 10, 20)))
    assert wrong.total == 0
    match = query_inspection_history(store, 1, filters(security_ids=["CN:600000", "CN:unknown"],
                trade_date_from=date(2026, 10, 20), trade_date_to=date(2026, 10, 20)))
    assert match.total == 1
    batch = match.items[0]
    assert batch.total_object_count == 2 and batch.matched_object_count == 1
    assert batch.matched_security_count == 1
    assert batch.review_decision == "approved" and batch.execution_result == "partial_failure"
    detail = get_inspection_history_batch(store, 1, f"audit-{first}", filters=match.filters)
    assert len(detail.objects) == 2 and len(detail.matched_objects) == 1
    assert detail.events[0].attributes["batch_id"] == "batch-1"
    assert detail.read_only is True


def test_approval_and_later_execution_failure_are_distinct_and_keep_report_version(store):
    first = event(store, corr="treatment", attrs={"review_decision": "approved", "review_note": "原始理由",
        "report_ref": "report-old", "report_version": "v2", "retention_status": "archived",
        "objects": [{"symbol_id": 1, "trade_date": "2026-10-19", "result": "not_executed"}]})
    event(store, corr="treatment", when=datetime(2026, 10, 22, 11),
          attrs={"execution_result": "failed", "task_id": "task-failed"})
    batch = query_inspection_history(store, 1, filters()).items[0]
    assert batch.event_count == 2 and batch.review_decision == "approved"
    assert batch.execution_result == "failed"
    detail = get_inspection_history_batch(store, 1, f"audit-{first}")
    report = next(ref for ref in detail.references if ref.kind == "report")
    assert (report.ref, report.version, report.retention_status) == ("report-old", "v2", "archived")
    assert detail.events[0].attributes["review_note"] == "原始理由"


@pytest.mark.parametrize("states,expected", [
    (["failed", "dependency_blocked"], "failed"),
    (["success", "failed"], "partial_failure"),
    (["success", "success"], "success"),
    (["success", "unknown"], "unknown"),
])
def test_batch_result_uses_complete_object_rechecks_and_zero_pass_is_failure(store, states, expected):
    event(store, attrs={"execution_result": "partial_failure", "objects": [
        {"symbol_id": 1, "trade_date": "2026-10-19", "result": states[0]},
        {"symbol_id": 2, "trade_date": "2026-10-19", "result": states[1]},
    ]})
    result = query_inspection_history(store, 1, filters())
    assert result.items[0].execution_result == expected


def test_portfolio_scope_precedes_grouping_details_references_and_security_search(store):
    own = event(store, attrs={"batch_id": "same", "issue_id": "current-issue"}, corr="same")
    other = event(store, pid=2, attrs={"batch_id": "same", "issue_id": "other-issue",
                 "objects": [{"symbol_id": 2, "trade_date": "2026-10-20"}]}, corr="same")
    result = query_inspection_history(store, 1, filters())
    assert result.total == 1 and result.items[0].event_count == 1
    assert result.items[0].batch_id == f"audit-{own}"
    assert get_inspection_history_batch(store, 1, f"audit-{other}") is None
    assert search_inspection_history_securities(store, 1).total == 0
    assert query_inspection_history(store, 1, filters(issue_id="other-issue")).relation_status == "no_related_history"


def test_group_before_page_dedup_retries_and_snapshot_watermark(store):
    first = event(store, attrs={"batch_id": "one", "objects": [{"symbol_id": 1, "trade_date": "2026-10-19", "result": "failed"}]}, corr="same")
    event(store, attrs={"batch_id": "one", "objects": [{"symbol_id": 1, "trade_date": "2026-10-19", "result": "success"}]}, corr="same")
    event(store, attrs={"batch_id": "two"}, when=datetime(2026, 10, 21, 11))
    page1 = query_inspection_history(store, 1, filters(), page_size=1)
    assert page1.total == 2 and page1.has_more
    assert page1.items[0].event_count == 2 and page1.items[0].total_object_count == 1
    assert page1.items[0].batch_id == f"audit-{first}"
    event(store, attrs={"batch_id": "new"}, when=datetime(2026, 10, 22, 12))
    page2 = query_inspection_history(store, 1, filters(), page=2, page_size=1, snapshot_event_id=page1.snapshot_event_id)
    assert page2.total == 2 and not page2.has_more
    assert page2.items[0].batch_id != page1.items[0].batch_id


def test_unknown_indexes_not_guessed_from_notes_occurrence_date_or_ready_state(store):
    event(store, attrs={"to_state": "READY"}, note="同意平安银行000001，2026-10-19处理成功")
    unfiltered = query_inspection_history(store, 1, filters())
    assert unfiltered.total == 1
    assert unfiltered.items[0].review_decision == "unknown"
    assert unfiltered.items[0].execution_result == "unknown"
    assert unfiltered.items[0].index_warnings
    assert query_inspection_history(store, 1, filters(security_ids=["CN:000001"])).total == 0
    assert query_inspection_history(store, 1, filters(trade_date_from=date(2026, 10, 22))).total == 0


def test_relation_matches_only_structured_refs_distinguishes_absent_from_filtered_out(store):
    event(store, attrs={"issue_id": "issue-1", "report_ref": "report-1", "report_version": "7",
                       "objects": [{"symbol_id": 1, "trade_date": "2026-10-19"}]}, note="issue-pretend")
    assert query_inspection_history(store, 1, filters(issue_id="issue-pretend")).relation_status == "no_related_history"
    actual = query_inspection_history(store, 1, filters(issue_id="issue-1", security_ids=["CN:600000"]))
    assert actual.relation_status == "filtered_out" and actual.empty_reason == "当前筛选无匹配记录"
    assert query_inspection_history(store, 1, filters(report_ref="report-1", report_version="7")).total == 1
    assert query_inspection_history(store, 1, filters(report_ref="report-1", report_version="8")).total == 0


def test_time_filter_independent_of_trade_date_and_history_name_code_search(store):
    event(store, symbol_id=1, attrs={"trade_date": "2026-09-01"})
    got = query_inspection_history(store, 1, HistoryFilters(
        occurred_from=datetime(2026, 10, 22, 10, tzinfo=timezone.utc),
        occurred_to=datetime(2026, 10, 22, 11, tzinfo=timezone.utc),
        trade_date_from=date(2026, 9, 1), trade_date_to=date(2026, 9, 1)))
    assert got.total == 1
    assert got.items[0].occurred_at.isoformat().endswith("+08:00")
    assert search_inspection_history_securities(store, 1, q="平安").items[0].security_id == "CN:000001"
    assert search_inspection_history_securities(store, 1, q="000001").total == 1


def test_real_repair_envelope_keeps_issue_refs_original_action_and_reinspection_failure(store):
    event(store, action="UNKNOWN_AUDIT_ACTION", attrs={
        "original_action": "REPAIR_TASK_RESULT", "event_family": "portfolio_inspection_repair",
        "issue_ids": ["issue-real"], "task_id": "task-1", "report_refs": ["async_task:task-1:reinspection"],
        "objects": [{"object_id": "repair-1", "symbol_id": 1, "trade_date": "2026-10-19",
                     "result": "succeeded", "reinspection_status": "STILL_BLOCKED"}],
    })
    result = query_inspection_history(store, 1, filters(issue_id="issue-real", security_ids=["CN:000001"],
        trade_date_from=date(2026, 10, 19), trade_date_to=date(2026, 10, 19), actions=["REPAIR_TASK_RESULT"]))
    assert result.total == 1 and result.items[0].title == "数据修复任务结果"
    assert result.items[0].execution_result == "failed"
    detail = get_inspection_history_batch(store, 1, result.items[0].batch_id)
    assert detail.events[0].source_action == "UNKNOWN_AUDIT_ACTION"
    assert detail.events[0].occurred_at.hour == 18 and detail.events[0].source_occurred_at.hour == 10
    assert detail.objects[0].result_basis == "reinspection"


def test_initial_coverage_personal_review_is_approved_without_claiming_execution_or_old_pass(store):
    event(store, attrs={"confirmation_type": "INITIAL_COVERAGE_REVIEW_CONFIRMED", "ack": True,
                       "baseline_id": "baseline-real", "trading_resumed": True, "reconciliation_passed": False})
    batch = query_inspection_history(store, 1, filters()).items[0]
    assert batch.title == "本人审核启用账户新范围"
    assert batch.review_decision == "approved" and batch.execution_result == "not_executed"
    assert not batch.objects


def test_material_is_bound_to_actual_reference_portfolio_version_and_watermark(store):
    own = event(store, attrs={"report_ref": "frozen-report", "report_version": "v1", "review_note": "原决定"})
    foreign = event(store, pid=2, attrs={"report_ref": "foreign-report"})
    material = get_inspection_history_material(store, 1, source_event_id=own, kind="report", ref="frozen-report", version="v1")
    assert material.status == "partial" and material.material_kind == "audit_snapshot"
    assert material.original_audit["attributes"]["review_note"] == "原决定"
    assert material.missing_sources == ["完整原报告 frozen-report / 版本 v1"]
    assert get_inspection_history_material(store, 1, source_event_id=own, kind="report", ref="frozen-report", version="v2") is None
    assert get_inspection_history_material(store, 1, source_event_id=foreign, kind="report", ref="foreign-report") is None
    assert get_inspection_history_material(store, 1, source_event_id=own, kind="report", ref="guessed-latest") is None
    assert get_inspection_history_material(store, 1, source_event_id=own, kind="report", ref="frozen-report", version="v1", snapshot_event_id=0) is None


def test_archived_material_is_not_replaced_by_current_source_and_credentials_are_redacted(store):
    own = event(store, attrs={"report_ref": "old", "retention_status": "archived", "api_key": "private-key",
        "source": {"password": "private-pass", "connection_string": "mysql://user:pass@server"}})
    material = get_inspection_history_material(store, 1, source_event_id=own, kind="report", ref="old")
    assert material.status == "archived" and material.data is None
    serialized = material.model_dump_json()
    assert "private-key" not in serialized and "private-pass" not in serialized and "user:pass" not in serialized
    assert "已脱敏" in serialized


def test_exact_retained_source_row_cannot_cross_portfolios_or_masquerade_as_snapshot(store):
    from sqlalchemy import text
    store.execute(text("CREATE TABLE sim_trades(id INTEGER PRIMARY KEY, portfolio_id INTEGER, quantity REAL, price REAL)"))
    store.execute(text("INSERT INTO sim_trades VALUES (1, 1, 2, 10), (2, 2, 99, 999)")); store.commit()
    own = event(store, attrs={"trade_ids": [1, 2]})
    source = get_inspection_history_material(store, 1, source_event_id=own, kind="trade", ref="1")
    assert source.status == "available" and source.material_kind == "retained_source"
    assert source.data["quantity"] == 2
    missing = get_inspection_history_material(store, 1, source_event_id=own, kind="trade", ref="2")
    assert missing.status == "missing" and missing.data is None


def test_export_selects_whole_query_before_pagination_keeps_full_objects_and_unknowns(store):
    own = event(store, attrs={"batch_id": "one", "issue_id": "current-issue", "report_ref": "missing-report",
        "objects": [{"symbol_id": 1, "trade_date": "2026-10-19", "result": "success"},
                    {"symbol_id": 2, "trade_date": "2026-10-19", "result": "failed"}], "secret": "do-not-export"})
    event(store, attrs={"batch_id": "two", "objects": [{"symbol_id": 1, "trade_date": "2026-10-19"}]})
    page = query_inspection_history(store, 1, filters(security_ids=["CN:000001"]), page_size=1)
    event(store, attrs={"batch_id": "too-new", "objects": [{"symbol_id": 1}]})
    package = export_inspection_history_evidence(store, 1, page.filters, snapshot_event_id=page.snapshot_event_id, actor="reviewer-1")
    assert package["payload"]["batch_count"] == 2
    batch = next(batch for batch in package["payload"]["batches"] if batch["batch_id"] == f"audit-{own}")
    assert batch["total_object_count"] == 2 and batch["matched_object_count"] == 1
    assert len(batch["objects"]) == 2 and len(batch["events"]) == 1
    assert package["payload"]["missing_evidence"] and package["payload"]["exceptions_and_unknowns"]
    assert "do-not-export" not in str(package)
    assert len(package["manifest"]["content_sha256"]) == 64
    assert store.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == 3


def test_actor_and_ai_filters_use_only_explicit_structured_metadata(store):
    event(store, attrs={"actor_type": "scheduler", "ai_assisted": False, "failure_code": "SOURCE_DOWN"})
    event(store, note="AI helped by scheduler SOURCE_DOWN")
    assert query_inspection_history(store, 1, filters(actor_type="scheduler", ai_assisted=False, failure_code="SOURCE_DOWN")).total == 1
    assert query_inspection_history(store, 1, filters(ai_assisted=True)).total == 0


def test_since_last_reconciliation_uses_scoped_passed_audit_not_latest_attempt_or_event_day(store):
    event(store, when=datetime(2026, 9, 1), action="RECONCILIATION_RESULT", attrs={"status": "PASSED", "trade_date": "2026-08-28"})
    event(store, when=datetime(2026, 10, 1), action="RECONCILIATION_RESULT", attrs={"status": "EVIDENCE_INCOMPLETE"})
    event(store, pid=2, when=datetime(2026, 10, 22), action="RECONCILIATION_RESULT", attrs={"status": "PASSED"})
    result = query_inspection_history(store, 1, filters(since_last_reconciliation=True))
    assert result.total == 2
    assert result.filters.occurred_from.isoformat() == "2026-09-01T08:00:00+08:00"


def test_configuration_review_has_no_applicable_trade_day_and_does_not_use_occurrence_day(store):
    event(store, action="FACTOR_USAGE_APPLIED", attrs={"confirmation_type": "MODEL_BINDING_CONFIRMED", "ack": True})
    assert query_inspection_history(store, 1, filters()).items[0].trade_date_index_status == "not_applicable"
    assert query_inspection_history(store, 1, filters(trade_date_from=date(2026, 10, 22))).total == 0


def test_explicit_common_coverage_interval_applies_only_to_objects_without_an_individual_date(store):
    event(store, attrs={"trade_date_from": "2026-10-01", "trade_date_to": "2026-10-20", "objects": [
        {"symbol_id": 1}, {"symbol_id": 2, "trade_date": "2026-09-01"}]})
    match = query_inspection_history(store, 1, filters(security_ids=["CN:000001"], trade_date_from=date(2026, 10, 15), trade_date_to=date(2026, 10, 15)))
    assert match.total == 1 and match.items[0].matched_object_count == 1
    assert match.items[0].matched_objects[0].trade_date_to == date(2026, 10, 20)
    assert query_inspection_history(store, 1, filters(security_ids=["CN:600000"], trade_date_from=date(2026, 10, 15), trade_date_to=date(2026, 10, 15))).total == 0


def test_exact_task_identity_groups_retry_events_without_time_coincidence(store):
    event(store, when=datetime(2026, 9, 1), attrs={"task_id": "task-real", "execution_result": "failed"}, corr="request-one")
    event(store, when=datetime(2026, 10, 1), attrs={"task_id": "task-real", "execution_result": "success"}, corr="request-two")
    event(store, when=datetime(2026, 10, 1), attrs={"task_id": "different-task"}, corr="request-three")
    result = query_inspection_history(store, 1, filters())
    assert result.total == 2
    assert next(batch for batch in result.items if batch.event_count == 2).execution_result == "success"


def test_g5_material_is_only_read_by_typed_reference_and_never_becomes_current_admission(store):
    import json
    from sqlalchemy import text
    from app.services.portfolio_inspection_evidence import _hash
    payload = {"portfolio_id": 1, "status": "COMPLETED", "g5_eligible_for_g6": True, "provenance": {"capture_mode": "synthetic"}}
    store.execute(text("CREATE TABLE g5_dual_run_reports(id INTEGER PRIMARY KEY, portfolio_id INTEGER, report_hash TEXT, report_json TEXT)"))
    store.execute(text("INSERT INTO g5_dual_run_reports VALUES (1, 1, :hash, :payload)"), {"hash": _hash(payload), "payload": json.dumps(payload)})
    store.commit()
    typed = event(store, attrs={"g5_report_id": 1})
    material = get_inspection_history_material(store, 1, source_event_id=typed, kind="report", ref="1")
    assert material.status == "available" and material.data["original_report"] == payload
    assert "不代表当前准入有效" in material.explanation
    generic = event(store, attrs={"report_id": 1})
    assert get_inspection_history_material(store, 1, source_event_id=generic, kind="report", ref="1").status == "partial"
    assert store.scalar(text("SELECT report_json FROM g5_dual_run_reports WHERE id=1")) == json.dumps(payload)


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: store
    with TestClient(app) as client:
        yield client


def test_http_contract_readonly_and_missing_portfolio(client, store):
    first = event(store, attrs={"confirmation_type": "MODEL_BINDING_CONFIRMED", "ack": True})
    before = store.scalar(select(func.count()).select_from(DataGovernanceAuditEvent))
    url = "/portfolios/1/inspection/history"
    got = client.get(url, params={"occurred_from": "2000-01-01T00:00:00"})
    assert got.status_code == 200
    payload = got.json()
    assert payload["source"] == "data_governance_audit_events"
    assert payload["items"][0]["review_decision"] == "approved"
    assert "events" not in payload["items"][0]
    assert client.get(f"{url}/audit-{first}").json()["events"][0]["id"] == first
    assert client.get("/portfolios/2/inspection/history/audit-1").status_code == 404
    assert client.get("/portfolios/999/inspection/history").status_code == 404
    assert client.post(url, json={}).status_code == 405
    assert client.get(url, params={"trade_date_from": "2026-10-22", "trade_date_to": "2026-10-21"}).status_code == 400
    assert client.get(url, params={"security_id": "000001"}).status_code == 400
    assert store.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == before


def test_http_read_failure_is_retryable_error_not_empty_history(client, monkeypatch):
    from sqlalchemy.exc import OperationalError
    def fail(*args, **kwargs):
        raise OperationalError("select", {}, RuntimeError("unavailable"))
    monkeypatch.setattr(routes, "query_inspection_history", fail)
    got = client.get("/portfolios/1/inspection/history")
    assert got.status_code == 503
    assert got.json()["detail"]["error_code"] == "HISTORY_READ_FAILED"
    assert "items" not in got.json()


def test_http_material_and_export_are_real_scoped_readonly_endpoints(client, store):
    own = event(store, attrs={"report_ref": "old-report", "report_version": "v1", "review_note": "保留原理由"})
    prefix = "/portfolios/1/inspection/history"
    response = client.get(prefix + "/material", params={"source_event_id": own, "kind": "report", "ref": "old-report", "version": "v1"})
    assert response.status_code == 200 and response.json()["status"] == "partial"
    assert client.get(prefix + "/material", params={"source_event_id": own, "kind": "report", "ref": "arbitrary"}).status_code == 404
    package = client.get(prefix + "/export", params={"occurred_from": "2000-01-01T00:00:00Z", "occurred_to": "2099-01-01T00:00:00Z"})
    assert package.status_code == 200 and package.json()["payload"]["batch_count"] == 1
    assert package.headers["content-disposition"].endswith('inspection-evidence.json"')
    assert package.headers["cache-control"] == "no-store"
    assert client.post(prefix + "/material").status_code == 405
    assert store.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == 1
