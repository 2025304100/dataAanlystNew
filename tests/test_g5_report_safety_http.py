"""Report truth and switching gates through actual HTTP and an isolated DB."""
import hashlib
import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import auto_trade, g5_dual_run
from app.db.session import get_db
from app.models.portfolio import Portfolio
from app.models.g5_dual_run import G5DualRunReport

pytestmark = pytest.mark.whitebox


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(g5_dual_run.router, prefix="/api/v1")
    app.include_router(auto_trade.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def portfolio(db_session):
    row = Portfolio(name="isolated-g5", account_type="simulated", asset_scope="mixed",
                    total_capital=100000, investable_ratio=1, cash_reserve_ratio=0,
                    last_decision_trade_date=date(2026, 8, 14), last_reconciled_trade_date=date(2026, 8, 14))
    db_session.add(row)
    db_session.commit()
    return row


def test_missing_report_is_not_a_passing_result(client, portfolio):
    response = client.get(f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/not-a-report")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "G5_REPORT_NOT_FOUND"
    assert "g5_eligible_for_g6" not in response.json()


def store_report(db, portfolio_id, payload, **overrides):
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row = G5DualRunReport(portfolio_id=portfolio_id, start_date=date(2026, 8, 3), end_date=date(2026, 8, 14),
        total_days=10, days_replayed=10, skipped_day_count=0, p0_unexplained_count=0,
        p1_hold_noaction_flip_count=0, eligible_for_g6=int(payload.get("g5_eligible_for_g6") is True),
        report_hash=hashlib.sha256(rendered.encode()).hexdigest(), report_json=rendered)
    for key, value in overrides.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def test_damaged_report_never_falls_back_to_completed_and_passing(client, portfolio, db_session):
    store_report(db_session, portfolio.id, {"replay_id": "damaged", "portfolio_id": portfolio.id,
        "start_date": "not-a-date", "g5_eligible_for_g6": True})
    response = client.get(f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/damaged")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "G5_REPORT_INVALID"
    assert "g5_eligible_for_g6" not in response.json()


def claimed_report(portfolio_id):
    dates = [f"2026-08-{day:02d}" for day in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14)]
    return {"portfolio_id": portfolio_id, "start_date": dates[0], "end_date": dates[-1],
        "total_days": 10, "days_replayed": 10, "skipped_days": [], "status": "COMPLETED",
        "total_p0_unexplained": 0, "total_p1_hold_noaction_flip": 0, "g5_eligible_for_g6": True,
        "avg_action_match_rate": 1.0, "avg_universe_jaccard": 1.0, "failing_days_p0": [], "failing_days_p1": [], "summary_notes": [],
        "daily_reports": [{"trade_date": day, "action_match_rate": 1.0, "universe_jaccard": 1.0,
            "p0_unexplained_count": 0, "p1_hold_noaction_flip": 0,
            "chain_a_meta": {"capture_mode": "legacy_dry_run", "source_run_id": f"old-{day}", "data_cutoff_at": f"{day}T15:00:00+08:00"},
            "chain_b_meta": {"capture_mode": "unified_dry_run", "source_run_id": f"new-{day}", "data_cutoff_at": f"{day}T15:00:00+08:00"}}
            for day in dates],
        "provenance": {"capture_mode": "real_dry_run_export", "exported_at": "2026-08-15T12:00:00+08:00",
            "trading_calendar": "trade_calendar", "source_manifest_sha256": "a" * 64, "expected_trade_dates": dates}}


def test_virtual_empty_report_cannot_be_used_by_any_switch_gate(client, portfolio, db_session):
    payload = claimed_report(portfolio.id)
    payload.update(replay_id="virtual", status="COMPLETED", avg_action_match_rate=1.0, avg_universe_jaccard=1.0,
                   failing_days_p0=[], failing_days_p1=[], summary_notes=[])
    payload["provenance"].update(capture_mode="historical_replay", simulated=True, real_dry_run=False,
                                 trading_calendar="G5_VIRTUAL_WEEKDAY")
    for day in payload["daily_reports"]:
        day.update(action_match_rate=1.0, universe_jaccard=1.0, p0_unexplained_count=0,
                   p1_hold_noaction_flip=0, action_confusion={})
        for chain in ("chain_a_meta", "chain_b_meta"):
            day[chain]["source_run_id"] = "g5-virtual-no-execution"
    store_report(db_session, portfolio.id, payload)
    base = f"/api/v1/portfolios/{portfolio.id}"
    response = client.get(base + "/g5-dual-run/virtual")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "G5_REPORT_INVALID"
    assert client.get(base + "/auto-trade/g6-readiness").json()["eligible"] is False
    assert client.get(base + "/auto-trade/switch-readiness").json()["can_switch"] is False
    assert client.get(base + "/auto-trade/g7-operational-status").json()["ready_for_expansion"] is False
    assert client.post(base + "/auto-trade/g6-start", json={"operator_id": "isolated-test"}).status_code == 409


def test_real_source_labels_without_actual_runs_are_not_execution_evidence(client, portfolio, db_session):
    payload = claimed_report(portfolio.id)
    payload["replay_id"] = "only-labels"
    store_report(db_session, portfolio.id, payload)
    base = f"/api/v1/portfolios/{portfolio.id}"
    response = client.get(base + "/g5-dual-run/only-labels")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "G5_REPORT_INVALID"
    assert client.get(base + "/auto-trade/g6-readiness").json()["eligible"] is False


def test_verified_decision_archive_is_readable_but_cannot_admit_an_unproved_complete_chain(client, portfolio, db_session):
    from tests.g5_report_fixtures import passing_g5
    payload = passing_g5(db_session, portfolio.id)
    store_report(db_session, portfolio.id, payload)
    base = f"/api/v1/portfolios/{portfolio.id}"
    response = client.get(base + "/g5-dual-run/" + payload["replay_id"])
    assert response.status_code == 200, response.text
    assert response.json()["evidence_verified"] is True
    assert response.json()["g5_eligible_for_g6"] is False
    assert response.json()["evidence_scope"] == "decision_actions_only"
    assert response.json()["acceptance_policy"]["admission_supported"] is False
    assert len(response.json()["acceptance_blockers"]) >= 4
    assert response.json()["daily_reports"][0]["action_confusion_matrix"]["HOLD"]["HOLD"] == 1
    readiness = client.get(base + "/auto-trade/g6-readiness")
    assert readiness.json()["eligible"] is False, readiness.text
    assert client.get(base + "/auto-trade/switch-readiness").json()["can_switch"] is False


def test_no_real_runner_disables_launch_without_creating_a_virtual_pass(client, portfolio):
    base = f"/api/v1/portfolios/{portfolio.id}/g5-dual-run"
    capabilities = client.get(base + "/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["real_runner_available"] is False
    response = client.post(base, json={"start_date": "2026-08-03", "end_date": "2026-08-14",
        "old_engine_version": "old", "new_engine_version": "new"})
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "G5_REAL_RUNNER_UNAVAILABLE"
    assert "replay_id" not in response.json()


@pytest.mark.parametrize("damage", ["hash", "typed_flag", "json_flag", "both_flags", "matrix", "last_day", "manifest", "portfolio", "missing_field"])
def test_damaged_proof_is_rejected_consistently_by_query_and_switch(client, portfolio, db_session, damage):
    from tests.g5_report_fixtures import passing_g5
    payload = passing_g5(db_session, portfolio.id)
    overrides = {}
    if damage == "hash": overrides["report_hash"] = "0" * 64
    elif damage == "typed_flag": overrides["eligible_for_g6"] = 1
    elif damage == "json_flag": payload["g5_eligible_for_g6"] = True; overrides["eligible_for_g6"] = 0
    elif damage == "both_flags": payload["g5_eligible_for_g6"] = True; overrides["eligible_for_g6"] = 1
    elif damage == "matrix": payload["daily_reports"][0]["action_confusion"]["HOLD"]["HOLD"] = 50
    elif damage == "last_day": payload["daily_reports"][-1]["action_match_rate"] = 0
    elif damage == "manifest": payload["provenance"]["source_manifest_sha256"] = "f" * 64
    elif damage == "portfolio": payload["portfolio_id"] = portfolio.id + 999
    elif damage == "missing_field": del payload["avg_action_match_rate"]
    store_report(db_session, portfolio.id, payload, **overrides)
    base = f"/api/v1/portfolios/{portfolio.id}"
    assert client.get(base + "/g5-dual-run/" + payload["replay_id"]).status_code == 409
    assert client.get(base + "/auto-trade/g6-readiness").json()["eligible"] is False
    assert client.get(base + "/auto-trade/switch-readiness").json()["can_switch"] is False
    assert client.get(base + "/auto-trade/g7-operational-status").json()["ready_for_expansion"] is False
    assert client.post(base + "/auto-trade/g6-start", json={"operator_id": "isolated-test"}).status_code == 409


@pytest.mark.parametrize("status", ["SUBMITTED", "RUNNING", "FAILED", "CANCELLED", "NOT_FOUND", "INVALID"])
def test_only_completed_report_can_be_a_passing_result(client, portfolio, db_session, status):
    from tests.g5_report_fixtures import passing_g5
    payload = passing_g5(db_session, portfolio.id)
    payload["status"] = status
    store_report(db_session, portfolio.id, payload)
    base = f"/api/v1/portfolios/{portfolio.id}"
    assert client.get(base + "/g5-dual-run/" + payload["replay_id"]).status_code == 409
    assert client.get(base + "/auto-trade/g6-readiness").json()["eligible"] is False


def test_latest_corrupt_report_never_falls_back_to_an_older_pass(client, portfolio, db_session):
    from tests.g5_report_fixtures import passing_g5
    payload = passing_g5(db_session, portfolio.id)
    store_report(db_session, portfolio.id, payload)
    store_report(db_session, portfolio.id, {}, report_json="{broken", report_hash="b" * 64)
    base = f"/api/v1/portfolios/{portfolio.id}"
    assert client.get(base + "/auto-trade/g6-readiness").json()["eligible"] is False
    assert client.get(base + "/auto-trade/switch-readiness").json()["can_switch"] is False
    assert client.post(base + "/auto-trade/g6-start", json={"operator_id": "isolated-test"}).status_code == 409


def test_report_of_another_portfolio_is_not_returned(client, portfolio, db_session):
    other = Portfolio(name="other-isolated", account_type="simulated", asset_scope="mixed", total_capital=100000,
                      investable_ratio=1, cash_reserve_ratio=0)
    db_session.add(other); db_session.commit()
    store_report(db_session, other.id, {"portfolio_id": other.id, "replay_id": "belongs-to-other"})
    assert client.get(f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/belongs-to-other").status_code == 404


@pytest.mark.parametrize("change", ["source", "empty", "calendar", "strategy"])
def test_original_source_changes_invalidate_a_previously_read_pass(client, portfolio, db_session, change):
    from sqlalchemy import text, update
    from app.models.decision_engine import DecisionRun, StrategyExecutionSnapshot
    from tests.g5_report_fixtures import passing_g5
    payload = passing_g5(db_session, portfolio.id)
    store_report(db_session, portfolio.id, payload)
    path = f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/" + payload["replay_id"]
    assert client.get(path).json()["evidence_verified"] is True
    assert client.get(path).json()["g5_eligible_for_g6"] is False
    if change in {"source", "empty"}:
        db_session.execute(update(DecisionRun).where(DecisionRun.id == payload["daily_reports"][0]["chain_b_meta"]["source_run_id"])
            .values(**({"status": "FAILED"} if change == "source" else {"universe_count": 0})))
    elif change == "calendar":
        db_session.execute(text("DELETE FROM trade_calendar WHERE date='2026-08-10'"))
    else:
        next_number = db_session.get(StrategyExecutionSnapshot, payload["applicable_scope"]["candidate_snapshot_id"]).snapshot_no + 1
        db_session.add(StrategyExecutionSnapshot(id="new-current-plan", portfolio_id=portfolio.id, snapshot_no=next_number,
            snapshot_type="save_and_apply", snapshot_hash="c" * 64, decision_clock_json="{}", member_snapshot_json="[]",
            effective_from=__import__("datetime").datetime(2026, 8, 15)))
    db_session.commit()
    assert client.get(path).status_code == 409
    assert client.get(f"/api/v1/portfolios/{portfolio.id}/auto-trade/g6-readiness").json()["eligible"] is False


def test_execution_using_a_later_days_data_is_not_valid_historical_proof(client, portfolio, db_session):
    from tests.g5_report_fixtures import passing_g5
    payload = passing_g5(db_session, portfolio.id, cutoff_offset_days=1)
    store_report(db_session, portfolio.id, payload)
    base = f"/api/v1/portfolios/{portfolio.id}"
    assert client.get(base + "/g5-dual-run/" + payload["replay_id"]).status_code == 409
    assert client.get(base + "/auto-trade/g6-readiness").json()["eligible"] is False


def test_storage_failure_is_unavailable_instead_of_a_report_or_a_pass(client, portfolio, db_session):
    from sqlalchemy import text
    db_session.execute(text("DROP TABLE g5_dual_run_reports"))
    db_session.commit()
    response = client.get(f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/unreadable")
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "G5_RESULT_UNAVAILABLE"
    assert "g5_eligible_for_g6" not in response.json()


@pytest.mark.parametrize("damage", ["no_status", "no_policy", "changed_policy", "sample_count", "old_coverage", "no_scope", "changed_build", "different_cutoff", "no_inputs", "self_input_hash", "calendar_version"])
def test_versioned_policy_scope_sample_and_input_contracts_are_mandatory(client, portfolio, db_session, damage):
    from tests.g5_report_fixtures import passing_g5
    payload = passing_g5(db_session, portfolio.id)
    if damage == "no_status": payload.pop("status")
    elif damage == "no_policy": payload.pop("acceptance_policy_version")
    elif damage == "changed_policy": payload["acceptance_policy"]["min_symbol_days"] = 0
    elif damage == "sample_count": payload["valid_symbol_days"] = 0
    elif damage == "old_coverage": payload["daily_reports"][0]["chain_a_coverage_pct"] = 0
    elif damage == "no_scope": payload.pop("applicable_scope")
    elif damage == "changed_build": payload["applicable_scope"]["candidate_engine_build_id"] = "some-other-deployment"
    elif damage == "different_cutoff": payload["daily_reports"][0]["chain_a_meta"]["data_cutoff_at"] = "2026-08-03T06:59:00"
    elif damage == "no_inputs": payload["daily_reports"][0].pop("input_manifest")
    elif damage == "self_input_hash": payload["daily_reports"][0]["chain_a_meta"]["input_manifest_sha256"] = hashlib.sha256(payload["replay_id"].encode()).hexdigest()
    elif damage == "calendar_version": payload["provenance"]["calendar_version"] = "guessed-weekdays"
    store_report(db_session, portfolio.id, payload)
    base = f"/api/v1/portfolios/{portfolio.id}"
    response = client.get(base + "/g5-dual-run/" + payload["replay_id"])
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reasons"]
    assert client.post(base + "/auto-trade/g6-start", json={"operator_id": "isolated-test"}).status_code == 409


@pytest.mark.parametrize("status,expected", [("queued", "SUBMITTED"), ("running", "RUNNING"), ("failed", "FAILED"), ("cancelled", "CANCELLED")])
def test_pending_report_queries_use_real_task_state_without_fake_statistics(client, portfolio, db_session, status, expected):
    from app.models.async_task import AsyncTaskRecord
    task = AsyncTaskRecord(id="real-pending-g5", task_type="g5_dual_run", status=status, stage=status,
        payload_json=json.dumps({"portfolio_id": portfolio.id, "replay_id": "pending-replay"}))
    db_session.add(task); db_session.commit()
    response = client.get(f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/pending-replay")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_status"] == body["status"] == expected
    assert body["g5_eligible_for_g6"] is body["evidence_verified"] is False
    assert body["start_date"] is body["avg_action_match_rate"] is body["total_days"] is None


def test_async_completed_claim_does_not_override_a_cancelled_real_task(client, portfolio, db_session):
    from tests.g5_report_fixtures import passing_g5
    from app.models.async_task import AsyncTaskRecord
    payload = passing_g5(db_session, portfolio.id)
    payload.update(execution_mode="async", async_task_id="cancelled-real-g5")
    db_session.add(AsyncTaskRecord(id="cancelled-real-g5", task_type="g5_dual_run", status="cancelled", stage="cancelled",
        payload_json=json.dumps({"portfolio_id": portfolio.id}), result_json=json.dumps({"replay_id": payload["replay_id"]})))
    store_report(db_session, portfolio.id, payload)
    assert client.get(f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/" + payload["replay_id"]).status_code == 409


def test_pending_archive_stub_uses_its_actual_task_and_does_not_invent_a_completed_report(client, portfolio, db_session):
    from app.models.async_task import AsyncTaskRecord
    payload = {"portfolio_id": portfolio.id, "replay_id": "pending-stub", "status": "SUBMITTED",
               "execution_mode": "async", "async_task_id": "running-real-task"}
    db_session.add(AsyncTaskRecord(id="running-real-task", task_type="g5_dual_run", status="running", stage="running",
        payload_json=json.dumps({"portfolio_id": portfolio.id, "replay_id": "pending-stub"})))
    store_report(db_session, portfolio.id, payload)
    response = client.get(f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/pending-stub")
    assert response.status_code == 200, response.text
    assert response.json()["job_status"] == "RUNNING"
    assert response.json()["evidence_verified"] is response.json()["g5_eligible_for_g6"] is False
    assert response.json()["avg_action_match_rate"] is response.json()["days_replayed"] is None


@pytest.mark.parametrize("change", ["bar", "score", "run_build", "accepted_baseline", "calendar_provenance"])
def test_archived_input_and_scope_revisions_are_detected_without_rewriting_the_original(client, portfolio, db_session, change):
    from tests.g5_report_fixtures import passing_g5
    from app.models.daily_bar import DailyBar
    from app.models.score import Score
    from app.models.decision_engine import DecisionRun
    from app.models.app_setting import AppSetting
    from app.services.data_governance_audit import write_audit_event
    payload = passing_g5(db_session, portfolio.id)
    row = store_report(db_session, portfolio.id, payload)
    original_json, original_hash = row.report_json, row.report_hash
    path = f"/api/v1/portfolios/{portfolio.id}/g5-dual-run/" + payload["replay_id"]
    assert client.get(path).json()["evidence_verified"] is True
    if change in {"bar", "score"}:
        ref = next(item for item in payload["daily_reports"][0]["input_manifest"] if item["source"] == ("daily_bars" if change == "bar" else "scores"))
        record = db_session.get(DailyBar if change == "bar" else Score, ref["record_id"])
        if change == "bar": record.close = 99
        else: record.quality_score = 99
    elif change == "run_build":
        record = db_session.get(DecisionRun, payload["daily_reports"][0]["chain_b_meta"]["source_run_id"])
        contract = json.loads(record.versions_json)
        contract["execution_contract"]["engine_build_id"] = "changed-after-capture"
        record.versions_json = json.dumps(contract)
    elif change == "accepted_baseline":
        write_audit_event(db_session, "FACTOR_USAGE_APPLIED", portfolio_id=portfolio.id,
            after={"accepted_binding": {"snapshot_id": payload["applicable_scope"]["candidate_snapshot_id"]}},
            attributes={"confirmation_type": "MODEL_BINDING_CONFIRMED"})
    else:
        state = db_session.get(AppSetting, "market.cn_trade_calendar")
        state.updated_by = "unverified-weekday-seed"
    db_session.commit()
    assert client.get(path).status_code == 409
    db_session.refresh(row)
    assert row.report_json == original_json
    assert row.report_hash == original_hash


def test_history_and_precheck_are_read_only_and_never_claim_an_available_runner(client, portfolio, db_session):
    from tests.g5_report_fixtures import passing_g5
    from sqlalchemy import event
    payload = passing_g5(db_session, portfolio.id)
    store_report(db_session, portfolio.id, payload)
    statements = []
    def observe(conn, cursor, sql, *args): statements.append(sql)
    event.listen(db_session.bind, "before_cursor_execute", observe)
    try:
        base = f"/api/v1/portfolios/{portfolio.id}/g5-dual-run"
        history = client.get(base + "/reports")
        precheck = client.post(base + "/precheck", json={"start_date": payload["start_date"], "end_date": payload["end_date"],
            "acceptance_policy_version": payload["acceptance_policy_version"]})
    finally:
        event.remove(db_session.bind, "before_cursor_execute", observe)
    assert history.status_code == 200, history.text
    assert history.json()["reports"][0]["replay_id"] == payload["replay_id"]
    assert history.json()["reports"][0]["evidence_verified"] is True
    assert precheck.status_code == 200
    assert precheck.json()["can_run"] is False
    assert precheck.json()["checks"][0]["reason_code"] == "G5_REAL_RUNNER_UNAVAILABLE"
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements)
