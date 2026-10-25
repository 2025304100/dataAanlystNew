"""Exercise the central mounted routes against isolated account evidence."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select, text

from app.api.routes import portfolio_governance as governance
from app.api.routes import portfolio_inspection_workflow as workflow
from app.db.session import get_db
from app.models.async_task import AsyncTaskRecord
from app.models.portfolio import Portfolio, Position
from app.models.scheduled_task import ScheduledTask
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.portfolio_state_machine import _get_status, _set_status
from tests.test_portfolio_inspection_reconciliation_services import DAY, seed_run

pytestmark = pytest.mark.whitebox


@pytest.fixture
def mounted(db_session, monkeypatch):
    from app.main import app
    from app.services import portfolio_reconciliation_coverage as coverage, portfolio_reconciliation_activation as activation
    from app.services import portfolio_inspection_runtime as runtime
    real_utc = coverage._utc
    frozen = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(coverage, "_utc", lambda value=None: real_utc(value or frozen))
    monkeypatch.setattr(activation, "_now", lambda: frozen.replace(tzinfo=None))
    monkeypatch.setattr(runtime, "_runtime_now", lambda: frozen)
    if "portfolio_status" not in {column["name"] for column in inspect(db_session.connection()).get_columns("portfolios")}:
        db_session.execute(text("ALTER TABLE portfolios ADD COLUMN portfolio_status VARCHAR(32)"))
    portfolio = Portfolio(name="central-http-inspection", account_type="simulated", total_capital=100000,
        investable_ratio=.95, cash_reserve_ratio=.05, effective_start_date=DAY, created_at=datetime(2026, 8, 1))
    db_session.add(portfolio)
    db_session.add(Symbol(id=1, symbol="CENTRAL-INSPECTION", name="Central HTTP Test", asset_type="stock", market="SH"))
    db_session.flush()
    db_session.add(CashLedger(portfolio_id=portfolio.id, entry_type="deposit", amount=100000,
        balance_after=100000, ref_type="portfolio", ref_id=portfolio.id, created_at=datetime(2026, 8, 1)))
    _set_status(db_session, portfolio, "READY")
    db_session.commit()
    monkeypatch.delenv("STRICT_AUTH", raising=False)
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield TestClient(app), db_session, portfolio
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def test_central_router_mounts_all_real_inspection_endpoints(mounted):
    client, _db, portfolio = mounted
    root = f"/api/v1/portfolios/{portfolio.id}"
    routes = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/portfolios/{portfolio_id}/inspection/data-diagnostics" in routes
    assert "/api/v1/portfolios/{portfolio_id}/inspection/repairs" in routes
    assert "/api/v1/portfolios/{portfolio_id}/inspection/runtime" in routes
    assert "/api/v1/portfolios/{portfolio_id}/inspection/reconciliation-queue" in routes
    assert client.get(root + "/inspection/data-diagnostics").status_code == 200
    runtime = client.get(root + "/inspection/runtime")
    assert runtime.status_code == 200
    assert runtime.json()["permissions"]["allow_risk_exits"] is True
    queue = client.get(root + "/inspection/reconciliation-queue")
    assert queue.status_code == 200
    assert queue.json()["baseline_creation_available"] is True
    assert queue.json()["coverage_source"] == "unknown"


@pytest.mark.parametrize("suffix", ["runtime", "reconciliation-queue"])
def test_runtime_and_queue_apply_strict_auth_and_the_shared_access_hook(mounted, monkeypatch, suffix):
    client, _db, portfolio = mounted
    url = f"/api/v1/portfolios/{portfolio.id}/inspection/{suffix}"
    monkeypatch.setenv("STRICT_AUTH", "1")
    assert client.get(url).status_code == 401
    calls = []
    def access(_db, pid, actor):
        calls.append((pid, actor))
        if actor != "authorized-reviewer":
            raise HTTPException(403, detail={"error_code": "FORBIDDEN_PORTFOLIO_ACCESS"})
    monkeypatch.setattr(workflow, "_ensure_portfolio_access", access)
    assert client.get(url + "?X-User=other-reviewer").status_code == 403
    assert client.get(url + "?X-User=authorized-reviewer").status_code == 200
    assert calls == [(portfolio.id, "other-reviewer"), (portfolio.id, "authorized-reviewer")]
    assert client.get(f"/api/v1/portfolios/999999/inspection/{suffix}?X-User=authorized-reviewer").status_code == 404


def test_preview_preserves_real_report_contract_and_does_not_flush_or_write(mounted):
    client, db, portfolio = mounted
    seed_run(db, portfolio)
    db.commit()
    portfolio.name = "pending-user-rename"
    db.add(Symbol(symbol="PENDING-HTTP-PREVIEW", name="Pending unrelated edit", asset_type="stock", market="SH"))
    statements = []
    def observe(_conn, _cursor, sql, *_args):
        statements.append(sql)
    event.listen(db.bind, "before_cursor_execute", observe)
    try:
        response = client.get(f"/api/v1/portfolios/{portfolio.id}/reconciliation-preview?trade_date={DAY.isoformat()}")
    finally:
        event.remove(db.bind, "before_cursor_execute", observe)
    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True
    assert body["report_id"]
    assert body["report_version"]
    assert body["verdict"] == "EVIDENCE_INCOMPLETE"
    assert any("历史持仓" in row["detail"] for row in body["missing_evidence"])
    assert "work_state" in body
    assert isinstance(body["reason_codes"], list)
    assert isinstance(body["missing_evidence"], list)
    assert isinstance(body["actions"], list)
    assert isinstance(body["progression"], dict)
    assert body["last_reconciled_trade_date"] is None
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE")) for sql in statements)
    assert list(db.scalars(select(DataGovernanceAuditEvent))) == []


def test_preview_preserves_missing_evidence_and_no_expected_cash_is_invented(mounted):
    client, _db, portfolio = mounted
    response = client.get(f"/api/v1/portfolios/{portfolio.id}/reconciliation-preview?trade_date={DAY.isoformat()}")
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "EVIDENCE_INCOMPLETE"
    assert body["missing_evidence"]
    assert body["actions"][0]["code"] == "INSPECT_EXECUTION"
    assert body["report_id"] and body["report_version"]
    assert body["expected_cash"] == body["actual_cash"] == 100000


def test_confirmation_of_stale_report_returns_409_and_preserves_reconciliation_lock(mounted):
    client, db, portfolio = mounted
    seed_run(db, portfolio)
    _set_status(db, portfolio, "RECONCILIATION_BLOCKED")
    portfolio.status_reconciliation = "RECONCILIATION_BLOCKED"
    db.commit()
    root = f"/api/v1/portfolios/{portfolio.id}"
    preview = client.get(root + f"/reconciliation-preview?trade_date={DAY.isoformat()}").json()
    assert preview["verdict"] == "EVIDENCE_INCOMPLETE"
    db.add(CashLedger(portfolio_id=portfolio.id, entry_type="deposit", amount=100,
        balance_after=100100, created_at=datetime(2026, 8, 21, 2)))
    db.commit()
    response = client.post(root + "/confirm-reconciliation", json={"trade_date": DAY.isoformat(),
        "acknowledge_all_diffs_cleared": True, "review_note": "已核实所审核版本的执行与账户记录",
        "report_id": preview["report_id"], "report_version": preview["report_version"]})
    assert response.status_code == 409
    assert "RECONCILIATION_REPORT_CHANGED" in response.text
    assert _get_status(db, portfolio) == "RECONCILIATION_BLOCKED"
    assert portfolio.last_reconciled_trade_date is None


def test_confirmation_requires_reviewed_report_identity_before_mutation(mounted):
    client, db, portfolio = mounted
    _set_status(db, portfolio, "RECONCILIATION_BLOCKED")
    portfolio.status_reconciliation = "RECONCILIATION_BLOCKED"
    db.commit()
    response = client.post(f"/api/v1/portfolios/{portfolio.id}/confirm-reconciliation", json={
        "trade_date": DAY.isoformat(), "acknowledge_all_diffs_cleared": True,
        "review_note": "本人核查完成但未选择具体报告版本"})
    assert response.status_code == 422
    assert portfolio.status_reconciliation == "RECONCILIATION_BLOCKED"


@pytest.mark.parametrize("method,path,payload", [
    ("get", "/reconciliation-preview", None), ("post", "/reconcile", {}),
])
def test_missing_calendar_never_guesses_yesterday(mounted, method, path, payload):
    client, _db, portfolio = mounted
    url = f"/api/v1/portfolios/{portfolio.id}{path}"
    response = client.get(url) if method == "get" else client.post(url, json=payload)
    assert response.status_code == 409
    assert response.json()["error_code"] == "RECONCILIATION_CALENDAR_UNKNOWN"


def test_generic_recovery_remark_is_forwarded_and_short_remark_is_422(mounted):
    client, db, portfolio = mounted
    _set_status(db, portfolio, "ADMIN_PAUSED")
    db.commit()
    root = f"/api/v1/portfolios/{portfolio.id}/transition-state"
    short = client.post(root, json={"to_state": "READY", "review_note": "太短"})
    assert short.status_code == 422
    note = "已核实当前组合账户与运行条件，批准恢复当前范围"
    response = client.post(root + "?X-User=actual-reviewer", json={"to_state": "READY", "review_note": note})
    assert response.status_code == 200
    row = list(db.scalars(select(DataGovernanceAuditEvent).order_by(DataGovernanceAuditEvent.id)))[-1]
    assert row.operator_id == "actual-reviewer"
    assert note in (row.attributes_json or "")


def _accept_current_coverage(mounted):
    from tests.test_portfolio_inspection import _calendar
    client, db, portfolio = mounted
    _calendar(db)
    db.commit()
    proposal = {"coverage_start_trade_date": "2026-10-23", "history_gaps": [{
        "trade_date_from": "2026-08-01", "trade_date_to": "2026-10-21",
        "reason": "历史来源经备份与审计检索确认无法恢复",
        "attempted_sources": ["历史备份", "旧运行审计"], "evidence_refs": ["backup:unrecoverable"]}]}
    root = f"/api/v1/portfolios/{portfolio.id}/inspection/coverage"
    preview = client.post(root + "/preview?X-User=actual-coverage-reviewer", json=proposal)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_accept"], preview.json()["blockers"]
    accepted = client.post(root + "/accept?X-User=actual-coverage-reviewer", json={**proposal,
        "expected_evidence_fingerprint": preview.json()["evidence_fingerprint"],
        "expected_cursor_row_version": preview.json()["cursor_row_version"],
        "acceptance_key": "central-http-coverage-accept", "review_note": "本人核实当前资产与义务并接受新的核验覆盖起点",
        "acknowledge_history_unknown": True})
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def _activation_payload(review, **updates):
    return {"baseline_id": review["baseline_id"], "generation": review["generation"],
        "expected_cursor_row_version": review["cursor_row_version"],
        "expected_evidence_fingerprint": review["evidence_fingerprint"],
        "activation_key": "central-http-coverage-activate", "acknowledge_new_scope": True,
        "review_note": "本人核实当前账户与全部独立门禁后启用新的执行范围", **updates}


def _book_state(db, portfolio_id):
    return {
        "cash": [tuple(row) for row in db.execute(select(CashLedger.id, CashLedger.amount, CashLedger.balance_after)
            .where(CashLedger.portfolio_id == portfolio_id).order_by(CashLedger.id))],
        "orders": [tuple(row) for row in db.execute(select(SimOrder.id, SimOrder.side, SimOrder.quantity, SimOrder.status)
            .where(SimOrder.portfolio_id == portfolio_id).order_by(SimOrder.id))],
        "trades": [tuple(row) for row in db.execute(select(SimTrade.id, SimTrade.side, SimTrade.quantity, SimTrade.amount)
            .where(SimTrade.portfolio_id == portfolio_id).order_by(SimTrade.id))],
        "positions": [tuple(row) for row in db.execute(select(Position.symbol_id, Position.quantity, Position.avg_cost)
            .where(Position.portfolio_id == portfolio_id).order_by(Position.symbol_id))],
    }


def test_accepted_account_snapshot_requires_separate_activation_and_preserves_unknown_history(mounted):
    client, db, portfolio = mounted
    before = _book_state(db, portfolio.id)
    accepted = _accept_current_coverage(mounted)
    assert accepted["runtime_enabled"] is False
    assert accepted["trading_resumed"] is False
    root = f"/api/v1/portfolios/{portfolio.id}/inspection"
    review = client.get(root + "/coverage/activation-review?X-User=actual-coverage-reviewer")
    assert review.status_code == 200
    body = review.json()
    assert body["can_confirm"] is True, body["blockers"]
    assert body["baseline_id"] == accepted["baseline_id"]
    assert body["already_activated"] is False
    assert body["reconciliation_passed"] is False
    runtime = client.get(root + "/runtime").json()
    assert runtime["permissions"]["allow_new_buys"] is False
    assert runtime["permissions"]["allow_risk_exits"] is True
    assert runtime["recovery_summary"]["confirmation_action"] == "INITIAL_COVERAGE_REVIEW"
    assert any("尚未" in message and "审核" in message for message in runtime["capability_reasons"]["allow_new_buys"])
    activate = client.post(root + "/coverage/activate?X-User=actual-coverage-reviewer", json=_activation_payload(body))
    assert activate.status_code == 200, activate.text
    assert activate.json()["reconciliation_passed"] is False
    assert activate.json()["historical_coverage_unknown"] is True
    assert _book_state(db, portfolio.id) == before
    assert portfolio.last_reconciled_trade_date is None
    again = client.post(root + "/coverage/activate?X-User=actual-coverage-reviewer", json=_activation_payload(body))
    assert again.status_code == 200
    assert again.json()["idempotent"] is True
    assert again.json()["audit_event_id"] == activate.json()["audit_event_id"]
    audit = db.get(DataGovernanceAuditEvent, activate.json()["audit_event_id"])
    assert audit.operator_id == "actual-coverage-reviewer"
    assert json.loads(audit.attributes_json)["baseline_id"] == accepted["baseline_id"]


@pytest.mark.parametrize("change,expected", [
    ({"expected_evidence_fingerprint": "0" * 64}, "ACTIVATION_EVIDENCE_CHANGED"),
    ({"expected_cursor_row_version": 99}, "COVERAGE_VERSION_CHANGED"),
    ({"generation": 99}, "COVERAGE_GENERATION_CHANGED"),
])
def test_activation_post_transmits_exact_conflict_codes_without_mutating_account(mounted, change, expected):
    client, db, portfolio = mounted
    _accept_current_coverage(mounted)
    root = f"/api/v1/portfolios/{portfolio.id}/inspection/coverage"
    review = client.get(root + "/activation-review?X-User=actual-coverage-reviewer").json()
    assert review["can_confirm"]
    before = _book_state(db, portfolio.id)
    audit_count = db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent))
    response = client.post(root + "/activate?X-User=actual-coverage-reviewer", json=_activation_payload(review, **change))
    assert response.status_code == 409
    assert response.json()["error_code"] == expected
    assert _book_state(db, portfolio.id) == before
    assert db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == audit_count


def test_activation_idempotency_key_reuse_error_is_not_hidden_by_central_error_wrapper(mounted):
    client, db, portfolio = mounted
    _accept_current_coverage(mounted)
    root = f"/api/v1/portfolios/{portfolio.id}/inspection/coverage"
    review = client.get(root + "/activation-review").json()
    payload = _activation_payload(review)
    first = client.post(root + "/activate?X-User=actual-coverage-reviewer", json=payload)
    assert first.status_code == 200, first.text
    count = db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent))
    changed = {**payload, "review_note": "同一请求键不能被再次用于不同的本人审核决定"}
    response = client.post(root + "/activate?X-User=actual-coverage-reviewer", json=changed)
    assert response.status_code == 409
    assert response.json()["error_code"] == "ACTIVATION_KEY_REUSED"
    assert db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == count


@pytest.mark.parametrize("suffix,method", [("coverage/activation-review", "get"), ("coverage/activate", "post"), ("reconciliation-job", "get")])
def test_new_workflow_routes_enforce_actor_and_shared_portfolio_access(mounted, monkeypatch, suffix, method):
    client, _db, portfolio = mounted
    url = f"/api/v1/portfolios/{portfolio.id}/inspection/{suffix}"
    payload = {"baseline_id": "not-an-existing-baseline", "generation": 1, "expected_cursor_row_version": 0,
        "expected_evidence_fingerprint": "0" * 64, "activation_key": "authorization-check", "acknowledge_new_scope": True,
        "review_note": "本人请求核实当前账户覆盖范围的访问权限"}
    def send(path):
        return client.get(path) if method == "get" else client.post(path, json=payload)
    monkeypatch.setenv("STRICT_AUTH", "1")
    assert send(url).status_code == 401
    calls = []
    def access(_db, pid, actor):
        calls.append((pid, actor))
        raise HTTPException(403, detail={"error_code": "FORBIDDEN_PORTFOLIO_ACCESS"})
    monkeypatch.setattr(workflow, "_ensure_portfolio_access", access)
    assert send(url + "?X-User=another-reviewer").status_code == 403
    assert calls == [(portfolio.id, "another-reviewer")]
    assert send(f"/api/v1/portfolios/999999/inspection/{suffix}?X-User=another-reviewer").status_code == 404


def test_reconciliation_job_reads_real_schedules_tasks_and_only_current_portfolio_items(mounted):
    client, db, portfolio = mounted
    now = datetime(2026, 10, 22, 9)
    db.add_all([
        ScheduledTask(name="actual-current-portfolio-schedule", task_type="portfolio_reconciliation", enabled=1,
            payload_json=json.dumps({"portfolio_id": portfolio.id}), next_run_at=now + timedelta(minutes=5)),
        ScheduledTask(name="unrelated-portfolio-schedule", task_type="portfolio_reconciliation", enabled=1,
            payload_json=json.dumps({"portfolio_id": 999999})),
        AsyncTaskRecord(id="actual-reconciliation-job", task_type="portfolio_reconciliation", status="done", stage="done",
            total=1, processed=1, percent=100, ok_count=0, failed_count=0, message="真实核对作业完成，业务仍待数据",
            created_at=now, payload_json=json.dumps({"portfolio_id": portfolio.id}), result_json=json.dumps({"items": [{
                "portfolio_id": portfolio.id, "verdict": "EVIDENCE_INCOMPLETE", "reason": "实际日期仍缺收盘持仓证据"}]})),
        AsyncTaskRecord(id="unrelated-reconciliation-job", task_type="portfolio_reconciliation", status="done", stage="done",
            created_at=now + timedelta(minutes=1), payload_json=json.dumps({"portfolio_id": 999999}), result_json="{}"),
        AsyncTaskRecord(id="shared-batch-reconciliation-job", task_type="portfolio_reconciliation", status="partial", stage="done",
            created_at=now - timedelta(minutes=1), payload_json=json.dumps({"portfolio_ids": [portfolio.id, 999999]}),
            result_json=json.dumps({"items": [{"portfolio_id": portfolio.id, "verdict": "READ_ERROR"},
                {"portfolio_id": 999999, "verdict": "PASSED", "reason": "PRIVATE_UNRELATED_RESULT"}]})),
    ])
    db.commit()
    statements = []
    def observe(_conn, _cursor, sql, *_args):
        statements.append(sql)
    event.listen(db.bind, "before_cursor_execute", observe)
    try:
        response = client.get(f"/api/v1/portfolios/{portfolio.id}/inspection/reconciliation-job")
    finally:
        event.remove(db.bind, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["task_id"] == "actual-reconciliation-job"
    assert body["status"] == "done"
    assert body["reason"] == "实际日期仍缺收盘持仓证据"
    assert [row["name"] for row in body["schedules"]] == ["actual-current-portfolio-schedule"]
    assert {row["id"] for row in body["jobs"]} == {"actual-reconciliation-job", "shared-batch-reconciliation-job"}
    assert all(item["portfolio_id"] == portfolio.id for row in body["jobs"] for item in row["items"])
    assert "PRIVATE_UNRELATED_RESULT" not in response.text
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE")) for sql in statements)


def test_direct_simulated_buy_is_blocked_before_any_account_mutation_when_coverage_not_activated(mounted, monkeypatch):
    from app.services import sim_accounts
    client, db, portfolio = mounted
    _accept_current_coverage(mounted)
    symbol = db.get(Symbol, 1)
    class AtCoverageStart(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 10, 23, 2, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(sim_accounts, "datetime", AtCoverageStart)
    before = _book_state(db, portfolio.id)
    with pytest.raises(HTTPException) as error:
        sim_accounts.place_sim_order(db, portfolio, symbol, "buy", 100, price=10,
            enforce_rules=False, apply_fees=False)
    assert error.value.status_code == 409
    assert error.value.detail["error_code"] == "INITIAL_COVERAGE_REVIEW_REQUIRED"
    assert _book_state(db, portfolio.id) == before


def test_direct_buy_without_coverage_preserves_legacy_behavior_and_real_sell_skips_only_coverage_guard(mounted, monkeypatch):
    from app.services import sim_accounts
    from app.services import portfolio_reconciliation_activation as activation
    _client, db, portfolio = mounted
    symbol = db.get(Symbol, 1)
    order, trade = sim_accounts.place_sim_order(db, portfolio, symbol, "buy", 100, price=10,
        enforce_rules=False, apply_fees=False)
    db.commit()
    assert order.side == trade.side == "buy"
    assert db.scalar(select(Position.quantity).where(Position.portfolio_id == portfolio.id)) == 100
    assert sim_accounts.cash_balance(db, portfolio.id) == 99000
    # A real risk-reducing sell must not call the new entry-only coverage gate.
    def reject_buy(*args, **kwargs):
        raise activation.CoverageError("INITIAL_COVERAGE_REVIEW_REQUIRED", "尚未审核启用")
    with patch.object(activation, "assert_execution_within_active_coverage", side_effect=reject_buy) as guard:
        sell_order, sell_trade = sim_accounts.place_sim_order(db, portfolio, symbol, "sell", 100, price=10,
            enforce_rules=False, apply_fees=False)
    db.commit()
    assert not guard.called
    assert sell_order.side == sell_trade.side == "sell"
    assert sim_accounts.cash_balance(db, portfolio.id) == 100000
    assert db.scalar(select(func.coalesce(func.sum(Position.quantity), 0)).where(Position.portfolio_id == portfolio.id)) == 0


def test_same_calendar_content_resync_keeps_accepted_baseline_review_and_fingerprint_valid(mounted):
    from app.models.app_setting import AppSetting
    client, db, portfolio = mounted
    _accept_current_coverage(mounted)
    root = f"/api/v1/portfolios/{portfolio.id}/inspection/coverage"
    before = client.get(root + "/activation-review").json()
    assert before["can_confirm"]
    setting = db.get(AppSetting, "market.cn_trade_calendar")
    source = json.loads(setting.value_json)
    source["refreshed_at"] = "2026-10-22T08:00:00+00:00"
    source["changed"] = 0
    setting.value_json = json.dumps(source)
    db.commit()
    after = client.get(root + "/activation-review").json()
    assert after["can_confirm"] is True, after["blockers"]
    assert after["baseline_id"] == before["baseline_id"]
    assert after["evidence_fingerprint"] == before["evidence_fingerprint"]
    response = client.post(root + "/activate?X-User=actual-coverage-reviewer", json=_activation_payload(before))
    assert response.status_code == 200, response.text


def test_activation_post_cannot_accept_another_portfolios_baseline_or_report(mounted):
    client, db, portfolio = mounted
    _accept_current_coverage(mounted)
    source = client.get(f"/api/v1/portfolios/{portfolio.id}/inspection/coverage/activation-review").json()
    other = Portfolio(name="other-http-activation-scope", account_type="simulated", total_capital=5000,
        investable_ratio=.95, cash_reserve_ratio=.05)
    db.add(other)
    db.commit()
    before = _book_state(db, other.id)
    count = db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent))
    response = client.post(f"/api/v1/portfolios/{other.id}/inspection/coverage/activate?X-User=actual-coverage-reviewer",
        json=_activation_payload(source))
    assert response.status_code == 409
    assert response.json()["error_code"] == "COVERAGE_GENERATION_CHANGED"
    assert _book_state(db, other.id) == before
    assert db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == count


def test_job_failure_without_per_portfolio_items_preserves_failure_instead_of_inventing_waiting(mounted):
    client, db, portfolio = mounted
    message = "核对作业读取来源失败，尚未形成逐日核对结果"
    db.add(ScheduledTask(name="enabled-failed-job-schedule", task_type="portfolio_reconciliation", enabled=1,
        payload_json=json.dumps({"portfolio_id": portfolio.id})))
    db.add(AsyncTaskRecord(id="failed-before-items", task_type="portfolio_reconciliation", status="failed", stage="failed",
        message=message, payload_json=json.dumps({"portfolio_id": portfolio.id}), result_json=json.dumps({"worker_crashed": True})))
    db.commit()
    response = client.get(f"/api/v1/portfolios/{portfolio.id}/inspection/reconciliation-job")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    # Unscoped worker text may be redacted by the account-isolation adapter.
    # The actual failed lifecycle must remain explicit, never become waiting.
    assert "失败" in body["reason"]
    assert "等待到期扫描" not in body["reason"]
    assert body["reason"] == body["jobs"][0]["message"]


def test_persistent_repair_history_and_cancel_use_actual_actor_and_scope_after_navigation(mounted, monkeypatch):
    client, db, portfolio = mounted
    db.add(AsyncTaskRecord(id="returnable-data-repair", task_type="portfolio_data_repair", status="queued", stage="queued",
        payload_json=json.dumps({"portfolio_id": portfolio.id, "actor": "original-submitter", "operations": [{
            "operation_id": "real-scoped-object", "symbol_id": 1, "symbol": "CENTRAL-INSPECTION", "kind": "sync_daily_bars",
            "trade_date": "2026-10-22", "depends_on": [], "resolves_evidence_ids": ["1:daily_bar"]}]})))
    db.add(AsyncTaskRecord(id="another-portfolio-repair", task_type="portfolio_data_repair", status="failed", stage="failed",
        payload_json=json.dumps({"portfolio_id": portfolio.id + 1, "operations": []})))
    db.commit()
    root = f"/api/v1/portfolios/{portfolio.id}/inspection/repairs"
    monkeypatch.setenv("STRICT_AUTH", "1")
    assert client.get(root).status_code == 401
    assert client.post(root + "/returnable-data-repair/cancel").status_code == 401
    checked = []
    def guard(_db, pid, actor):
        checked.append((pid, actor))
        if actor != "repair-reviewer":
            raise HTTPException(403, detail={"error_code": "FORBIDDEN_PORTFOLIO_ACCESS"})
    monkeypatch.setattr(governance, "_ensure_portfolio_access", guard)
    assert client.get(root + "?X-User=another-reviewer").status_code == 403
    before = db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent))
    history = client.get(root + "?X-User=repair-reviewer&limit=1")
    assert history.status_code == 200
    assert [item["task_id"] for item in history.json()["items"]] == ["returnable-data-repair"]
    assert history.json()["items"][0]["created_by"] == "original-submitter"
    assert db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == before
    assert client.post(root + "/another-portfolio-repair/cancel?X-User=repair-reviewer").status_code == 404
    cancelled = client.post(root + "/returnable-data-repair/cancel?X-User=repair-reviewer")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["retryable_operation_ids"] == ["real-scoped-object"]
    again = client.post(root + "/returnable-data-repair/cancel?X-User=repair-reviewer")
    assert again.json()["replayed"] is True
    row = db.scalar(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.business_key == "returnable-data-repair"))
    assert row.operator_id == "repair-reviewer"
    assert json.loads(row.attributes_json)["original_action"] == "REPAIR_TASK_CANCEL_REQUESTED"
    assert (portfolio.id, "another-reviewer") in checked


@pytest.mark.parametrize("effective_microsecond,publication_microsecond,passed,status", [
    (700000,700000,True,"PASSED"),
    (900000,700000,False,"UNKNOWN"),
    (700000,900000,False,"FUTURE_DATA"),
])
def test_fractional_binding_clock_is_consistent_across_real_http_reads(mounted, monkeypatch, effective_microsecond, publication_microsecond, passed, status):
    from freezegun import freeze_time
    from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
    from app.services.auto_trade_safety import check_data_health
    from tests.test_portfolio_inspection import _seed, _bar, _score, NOW
    from tests.test_stage3_factor_binding_contract import _activate, _ready_model
    from tests.g4_contract_support import financial_rows
    client, db, _existing = mounted
    portfolio, config, symbols = _seed(db, count=1)
    model = _ready_model(db, set_id="fs-fraction-http", model_id="model-fraction-http")
    _activate(db, model.id)
    stamp = NOW.replace(tzinfo=None, microsecond=effective_microsecond)
    usage = PortfolioFactorUsage(id="fraction-usage", portfolio_id=portfolio.id,
        factor_model_run_id=model.id, factor_set_id="fs-fraction-http", status="active",
        run_mode="production_sim", pit_mode="strict_pit_safe", content_hash="fraction-input",
        effective_from=stamp)
    db.add(usage); db.flush()
    db.add(StrategyExecutionSnapshot(id="fraction-snapshot", portfolio_id=portfolio.id,
        portfolio_factor_usage_id=usage.id, factor_model_run_id=model.id, factor_set_id="fs-fraction-http",
        snapshot_no=1, snapshot_type="save_and_apply", snapshot_hash="fraction-proof",
        member_snapshot_json="[]", decision_clock_json="{}", effective_from=stamp))
    _bar(db, symbols[0], NOW.date())
    value=_score(db, symbols[0], config, NOW.date(), weight_mode="ridge",
        factor_model_run_id=model.id, factor_set_id="fs-fraction-http")
    value.published_at=NOW.replace(tzinfo=None,microsecond=publication_microsecond)
    portfolio.status_model="MODEL_INACTIVE"
    db.commit()
    before=financial_rows(db)
    at=NOW.replace(microsecond=800000)
    monkeypatch.setattr("app.services.portfolio_inspection_runtime._runtime_now", lambda: at)
    with freeze_time(at.isoformat(), real_asyncio=True):
        root=f"/api/v1/portfolios/{portfolio.id}"
        review=client.get(root+"/model-binding-review")
        assert review.status_code==200 and review.json()["can_confirm"] is (effective_microsecond < 800000)
        health=check_data_health(db, symbol_id=symbols[0].id, portfolio_id=portfolio.id)
        assert health.healthy is passed,health.reason
        response=client.get(root+"/inspection/data-diagnostics")
        assert response.status_code==200,response.text
        inspection=response.json()
        score=next(e for e in inspection["symbols"][0]["evidence"] if e["check_code"]=="SCORE_FRESHNESS")
        assert score["status"] == status,score
        assert datetime.fromisoformat(inspection["generated_at"].replace("Z","+00:00"))==at
        runtime=client.get(root+"/inspection/runtime")
        assert runtime.status_code==200,runtime.text
        data=next(c for c in runtime.json()["recovery_summary"]["checks"] if c["key"]=="data")
        assert data["passed"] is (True if passed else None if status=="UNKNOWN" else False),data
    assert financial_rows(db)==before
