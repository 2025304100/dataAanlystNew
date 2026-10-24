"""Inspection recovery must be evidence-driven at the real service seam."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services.portfolio_inspection_runtime import get_portfolio_runtime_inspection
from app.services.portfolio_reconciliation import reconcile_trade_date
from app.services.portfolio_resume_service import _next_trade_date, get_reconciliation_queue
from app.services.portfolio_state_machine import (
    PortfolioStateMachine, StateTransitionError, _get_status, _set_status,
    confirm_reconciliation_fixed, transition_portfolio_state,
)

pytestmark = pytest.mark.whitebox
DAY = date(2026, 8, 20)


@pytest.fixture
def account():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE portfolios ADD COLUMN portfolio_status VARCHAR(32)"))
    with Session(engine, expire_on_commit=False) as db:
        p = Portfolio(name="inspection-recovery", account_type="simulated", total_capital=100000,
            investable_ratio=.95, cash_reserve_ratio=.05, effective_start_date=DAY,
            created_at=datetime(2026, 8, 1))
        db.add(p)
        db.add(Symbol(id=1, symbol="INSPECTION", name="Test", asset_type="stock", market="SH"))
        db.flush()
        db.add(CashLedger(portfolio_id=p.id, entry_type="deposit", amount=100000,
            balance_after=100000, ref_type="portfolio", ref_id=p.id,
            note="Initial simulated funding", created_at=datetime(2026, 8, 1)))
        _set_status(db, p, "READY")
        db.commit()
        yield db, p
    engine.dispose()


def seed_run(db, p, *, status="SUCCEEDED", execution_at=datetime(2026, 8, 21, 1, 30)):
    snapshot = StrategyExecutionSnapshot(id="inspection-snapshot", snapshot_no=1,
        portfolio_id=p.id, decision_clock_json="{}", member_snapshot_json="[]",
        snapshot_hash=hashlib.sha256(b"inspection").hexdigest(), effective_from=datetime(2026, 8, 20))
    db.add(snapshot)
    db.flush()
    run = DecisionRun(id="inspection-run", strategy_snapshot_id=snapshot.id, portfolio_id=p.id,
        run_type="auto_simulation", status=status, trade_date=DAY,
        decision_at=datetime(2026, 8, 20, 12, 30), data_cutoff_at=datetime(2026, 8, 20, 12),
        execution_at=execution_at, universe_count=1, member_count=1)
    db.add(run)
    db.flush()
    db.add(DecisionEvidence(id="inspection-evidence", decision_run_id=run.id,
        strategy_snapshot_id=snapshot.id, portfolio_id=p.id, symbol_id=1, trade_date=DAY,
        decision_at=run.decision_at, data_cutoff_at=run.data_cutoff_at, execution_at=execution_at,
        action="HOLD", min_lot_size=100, target_quantity=0, target_qty_delta=0,
        content_hash=hashlib.sha256(b"inspection-evidence").hexdigest()))
    p.last_decision_trade_date = DAY
    db.flush()
    return run


def block(db, p):
    _set_status(db, p, "RECONCILIATION_BLOCKED")
    p.status_reconciliation = "RECONCILIATION_BLOCKED"
    db.flush()


def events(db):
    return list(db.scalars(select(DataGovernanceAuditEvent).order_by(DataGovernanceAuditEvent.id)))


@pytest.mark.parametrize("call", [
    lambda db, p: transition_portfolio_state(db, p.id, "READY"),
    lambda db, p: PortfolioStateMachine.transition(db=db, portfolio_id=p.id,
        from_state="READY", to_state="READY"),
])
def test_generic_ready_cannot_clear_reconciliation_or_lie_about_source_state(account, call):
    db, p = account
    block(db, p)
    with pytest.raises(StateTransitionError, match="RECONCILIATION_CONFIRMATION_REQUIRED"):
        call(db, p)
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED"


def test_dimension_lock_also_guards_runtime_ready_noop(account):
    db, p = account
    p.status_reconciliation = "RECONCILIATION_BLOCKED"
    with pytest.raises(StateTransitionError, match="RECONCILIATION_CONFIRMATION_REQUIRED"):
        transition_portfolio_state(db, p.id, "READY")


def test_successful_transition_retains_review_note_without_duplicate_noop_audit(account):
    db, p = account
    transition_portfolio_state(db, p.id, "ADMIN_PAUSED", review_note="本人暂停以核实当前持仓情况")
    with pytest.raises(ValueError, match="审核备注"):
        transition_portfolio_state(db, p.id, "READY")
    note = "已核实暂停期间的账户与运行条件，恢复当前范围"
    transition_portfolio_state(db, p.id, "READY", operator_id="reviewer", review_note=note)
    transition_portfolio_state(db, p.id, "READY", operator_id="reviewer", review_note=note)
    rows = events(db)
    assert len(rows) == 2
    assert json.loads(rows[-1].attributes_json)["review_note"] == note
    assert rows[-1].operator_id == "reviewer"
    assert json.loads(rows[-1].attributes_json)["confirmation_type"] == "STATE_TRANSITION"


def test_missing_run_is_incomplete_evidence_with_a_concrete_action(account):
    db, p = account
    report = reconcile_trade_date(db, p.id, DAY)
    assert report.status == "BLOCKED"
    assert report.verdict == "EVIDENCE_INCOMPLETE"
    assert report.actions[0]["code"] == "INSPECT_EXECUTION"
    assert report.missing_evidence
    assert report.expected_position_count is None and report.actual_position_count is None
    assert report.expected_cash == report.actual_cash == 100000
    assert p.last_reconciled_trade_date is None
    assert p.status_reconciliation == "RECONCILIATION_BLOCKED"


@pytest.mark.parametrize("status", ["SUCCEEDED", "RUNNING", "PENDING"])
def test_not_yet_executed_is_waiting_and_does_not_invent_a_pause(account, status):
    db, p = account
    seed_run(db, p, status=status)
    report = reconcile_trade_date(db, p.id, DAY, now=datetime(2026, 8, 20, 13))
    assert report.work_state == "waiting_execution"
    assert report.verdict in {"NOT_DUE", "EXECUTION_IN_PROGRESS"}
    assert report.actions[0]["code"] == "VIEW_EXECUTION_PROGRESS"
    assert _get_status(db, p) == "READY"
    assert p.status_reconciliation == "READY"
    assert p.last_reconciled_trade_date is None


def test_actual_cash_difference_takes_priority_over_waiting(account):
    db, p = account
    seed_run(db, p)
    db.add(CashLedger(portfolio_id=p.id, entry_type="withdraw", amount=-50,
        balance_after=100000, created_at=datetime(2026, 8, 20)))
    db.flush()
    report = reconcile_trade_date(db, p.id, DAY, now=datetime(2026, 8, 20, 13))
    assert report.verdict == "DIFF_FOUND"
    assert report.work_state == "needs_review"
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED"


def test_confirmation_rejects_changed_evidence_even_if_new_balance_is_consistent(account):
    db, p = account
    seed_run(db, p)
    block(db, p)
    preview = reconcile_trade_date(db, p.id, DAY, persist=False)
    assert preview.status == "PASSED"
    db.add(CashLedger(portfolio_id=p.id, entry_type="deposit", amount=100,
        balance_after=100100, created_at=datetime(2026, 8, 21, 2)))
    db.flush()
    with pytest.raises(StateTransitionError, match="RECONCILIATION_REPORT_CHANGED"):
        confirm_reconciliation_fixed(db, p.id, DAY, operator_id="reviewer",
            acknowledge_all_diffs_cleared=True, review_note="核实当前核对报告与实际账户资金", 
            report_id=preview.report_id, report_version=preview.report_version)
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED"
    assert p.last_reconciled_trade_date is None


def test_confirmation_only_clears_reconciliation_and_audits_exact_report(account):
    db, p = account
    seed_run(db, p)
    block(db, p)
    p.status_data = "DATA_INCOMPLETE_PAUSED"
    p.status_model = "MODEL_INACTIVE"
    db.flush()
    preview = reconcile_trade_date(db, p.id, DAY, persist=False)
    result = confirm_reconciliation_fixed(db, p.id, DAY, operator_id="reviewer",
        acknowledge_all_diffs_cleared=True, review_note="已核实本日执行资金与持仓均一致",
        report_id=preview.report_id, report_version=preview.report_version)
    assert p.status_reconciliation == "READY"
    assert p.status_data == "DATA_INCOMPLETE_PAUSED"
    assert p.status_model == "MODEL_INACTIVE"
    assert result.portfolio_now_ready is False
    attrs = json.loads(events(db)[-1].attributes_json)
    assert attrs["report_version"] == preview.report_version
    assert attrs["review_note"] == "已核实本日执行资金与持仓均一致"


def test_confirming_old_clean_day_does_not_clear_another_date(account):
    db, p = account
    seed_run(db, p)
    block(db, p)
    write_audit_event(db, "RECONCILIATION_RESULT", portfolio_id=p.id,
        attributes={"trade_date": "2026-08-22", "status": "BLOCKED", "work_state": "needs_review"})
    result = confirm_reconciliation_fixed(db, p.id, DAY, operator_id="reviewer",
        acknowledge_all_diffs_cleared=True)
    assert not result.portfolio_now_ready
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED"
    assert p.status_reconciliation == "RECONCILIATION_BLOCKED"


def test_later_clean_report_cannot_jump_an_established_checkpoint(account):
    db, p = account
    seed_run(db, p)
    p.last_reconciled_trade_date = date(2026, 8, 18)
    db.add_all([MarketCalendar(trade_date=date(2026, 8, day), is_trading_day=1) for day in (19, 20)])
    db.flush()
    report = reconcile_trade_date(db, p.id, DAY)
    assert report.status == "PASSED"
    assert p.last_reconciled_trade_date == date(2026, 8, 18)
    assert report.progression["reason"] == "PREVIOUS_DATE_UNVERIFIED"


def test_runtime_legacy_guide_targets_queue_gap_instead_of_later_audit(account, monkeypatch):
    db, p = account
    seed_run(db, p)
    p.last_reconciled_trade_date = DAY - timedelta(days=2)
    reconcile_trade_date(db, p.id, DAY)
    db.commit()
    from app.services import portfolio_inspection_diagnostics as diagnostics
    monkeypatch.setattr(diagnostics, 'resolve_inspection_calendar', lambda db: {
        'status': 'PASSED', 'expected_trade_date': DAY, 'version': 'fixture-calendar',
        'trading_dates': [DAY - timedelta(days=1), DAY], 'reason': 'Isolated authoritative adapter result'})
    queue = get_reconciliation_queue(db, p.id)
    runtime = get_portfolio_runtime_inspection(db, p.id)
    proof = runtime['guide']['steps'][0]['proof']['check']
    assert queue['target_trade_date'] == (DAY - timedelta(days=1)).isoformat()
    assert proof['trade_date'] == queue['target_trade_date']
    assert proof['report_id'] is None
    assert not runtime['guide']['can_confirm']


def test_queue_deployment_capabilities_read_actual_scoped_schedules(account):
    from app.models.scheduled_task import ScheduledTask
    from app.services.portfolio_reconciliation_coverage import get_reconciliation_coverage
    db, p = account
    assert get_reconciliation_queue(db, p.id)['baseline_creation_available']
    assert not get_reconciliation_queue(db, p.id)['automatic_job_registered']
    assert get_reconciliation_coverage(db, p.id)['historical_coverage_unknown']
    row = ScheduledTask(name='Other account', task_type='portfolio_reconciliation', enabled=1,
        frequency='interval', interval_minutes=5, payload_json=json.dumps({'portfolio_id': p.id + 1}))
    db.add(row); db.flush()
    assert not get_reconciliation_queue(db, p.id)['automatic_job_registered']
    row.payload_json = json.dumps({'portfolio_id': p.id}); db.flush()
    assert get_reconciliation_queue(db, p.id)['automatic_job_registered']
    row.enabled = 0; db.flush()
    assert not get_reconciliation_queue(db, p.id)['automatic_job_registered']


def test_checkpoint_does_not_turn_legacy_unknown_audits_into_new_blocking_dates(account):
    db, p = account
    write_audit_event(db, 'RECONCILIATION_RESULT', portfolio_id=p.id,
        attributes={'trade_date': DAY.isoformat()}, operator_id='legacy-user')
    p.last_reconciled_trade_date = DAY
    db.commit()
    item = next(row for row in get_reconciliation_queue(db, p.id)['days'] if row['trade_date'] == DAY.isoformat())
    assert item['history_only']
    assert item['status'] == 'HISTORICAL_EVIDENCE_UNKNOWN'
    assert item['work_state'] == 'history_unknown'


def test_new_check_audit_uses_explicit_utc_and_is_found_by_current_report_history(account):
    from app.schemas.portfolio_inspection_history import HistoryFilters
    from app.services.portfolio_inspection_history import query_inspection_history
    db, p = account
    report = reconcile_trade_date(db, p.id, DAY)
    db.commit()
    audit = db.get(DataGovernanceAuditEvent, report.audit.event_id)
    assert json.loads(audit.attributes_json)['source_timezone'] == 'UTC'
    assert abs((audit.occurred_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 10
    page = query_inspection_history(db, p.id, HistoryFilters(trade_date_from=DAY, trade_date_to=DAY,
        report_ref=report.report_id, report_version=report.report_version))
    assert page.total == 1
    assert page.relation_status == 'matched'
    assert page.items[0].occurred_at <= datetime.now(timezone.utc)


def test_runtime_permissions_derive_all_dimensions_and_are_read_only(account):
    db, p = account
    p.status_data = "DATA_INCOMPLETE_PAUSED"
    p.status_model = "MODEL_INACTIVE"
    db.flush()
    before = len(events(db))
    runtime = get_portfolio_runtime_inspection(db, p.id)
    assert runtime["permissions"]["allow_new_buys"] is False
    assert runtime["permissions"]["allow_risk_exits"] is True
    assert runtime["permissions"]["allow_auto_recovery"] is False
    assert len(runtime["capability_reasons"]["allow_new_buys"]) == 2
    assert runtime["recovery_summary"]["can_confirm"] is False
    assert len(events(db)) == before
    assert _get_status(db, p) == "READY"


def test_runtime_invalid_dimension_is_unknown_not_allowed(account):
    db, p = account
    p.status_data = None
    runtime = get_portfolio_runtime_inspection(db, p.id)
    assert runtime["permissions"]["allow_new_buys"] is None
    assert runtime["permissions"]["allow_risk_exits"] is None


def test_persisted_invalid_runtime_state_is_unknown_not_legacy_ready(account):
    db, p = account
    p.last_reconciled_trade_date = DAY
    db.execute(text("UPDATE portfolios SET portfolio_status='BROKEN_RUNTIME' WHERE id=:id"), {"id": p.id})
    runtime = get_portfolio_runtime_inspection(db, p.id)
    assert runtime["current_state"] == "BROKEN_RUNTIME"
    assert runtime["permissions"]["allow_new_buys"] is None
    assert runtime["permissions"]["allow_auto_recovery"] is None
    assert not runtime["recovery_summary"]["can_confirm"]


def test_current_rule_binding_failure_blocks_persisted_green_review(account, monkeypatch):
    db, p = account
    _set_status(db, p, "ADMIN_PAUSED")
    import app.services.portfolio_inspection_diagnostics as diagnostics
    monkeypatch.setattr(diagnostics, "build_portfolio_inspection", lambda *a, **kw: {
        "snapshot_id": "invalid-rule", "symbols": [], "evidence": [{
            "check_code": "RULE_BINDING", "status": "VERSION_MISMATCH", "reason": "当前策略规则已停用", "admission_relevant": True}]})
    runtime = get_portfolio_runtime_inspection(db, p.id)
    assert not runtime["recovery_summary"]["can_confirm"]
    binding = next(c for c in runtime["recovery_summary"]["checks"] if c["key"] == "binding")
    assert binding["passed"] is False
    assert "当前策略规则已停用" in binding["reason"]


def test_queue_does_not_invent_baseline_or_weekday_dates(account):
    db, p = account
    assert _next_trade_date(db, date(2026, 9, 30)) is None
    queue = get_reconciliation_queue(db, p.id, today=DAY)
    assert queue["coverage_start"] is None
    assert queue["calendar_status"] == "UNKNOWN"
    assert queue["days"] == []
    assert "NO_ACCEPTED_ACCOUNT_BASELINE" in queue["limitations"]
    assert queue["baseline_creation_available"]


@pytest.mark.parametrize("state,action", [
    ("PENDING_INITIAL_REVIEW", "INITIAL_REVIEW"),
    ("ADMIN_PAUSED", "RESUME_ADMIN_PAUSE"),
    ("INTERRUPTED", "REVIEW_INTERRUPTED"),
])
def test_manual_recovery_states_have_an_applicable_review_exit(account, state, action):
    db, p = account
    _set_status(db, p, state)
    runtime = get_portfolio_runtime_inspection(db, p.id)
    assert runtime["recovery_summary"]["can_confirm"] is True
    assert runtime["recovery_summary"]["confirmation_action"] == action
    check = next(c for c in runtime["recovery_summary"]["checks"] if c["key"] == "reconciliation")
    assert check["not_applicable"] is True
    assert check["report_id"] is None
    transition_portfolio_state(db, p.id, "READY", operator_id="reviewer",
        review_note="已核实当前范围和运行条件，批准恢复")
    assert _get_status(db, p) == "READY"


def test_risk_exit_denied_reason_never_claims_it_is_allowed(account, monkeypatch):
    db, p = account
    import app.services.portfolio_inspection_runtime as runtime_module
    original = runtime_module._check_allows_action
    monkeypatch.setattr(runtime_module, "_check_allows_action",
        lambda state, action: False if action == "RISK_SELL" else original(state, action))
    result = get_portfolio_runtime_inspection(db, p.id)
    assert result["permissions"]["allow_risk_exits"] is False
    assert "禁止风险退出" in result["capability_reasons"]["allow_risk_exits"][0]


def test_activated_future_scope_waits_for_start_without_disabling_risk_exit(account, monkeypatch):
    db, p = account
    import app.services.portfolio_inspection_runtime as runtime
    import app.services.portfolio_reconciliation as reconciliation
    import app.services.portfolio_reconciliation_activation as activation
    start = date(2026, 10, 23)
    active = {"baseline_id": "baseline-reviewed", "generation": 1, "cursor_id": "cursor-reviewed",
        "coverage_start_trade_date": start, "verified_through_trade_date": None,
        "row_version": 1, "calendar_version": "known", "history_gaps": [], "baseline_snapshot": {}}
    monkeypatch.setattr(reconciliation, "_active_coverage", lambda *a, **kw: active)
    monkeypatch.setattr(activation, "get_initial_coverage_activation", lambda *a, **kw: {"audit_id": 1})
    monkeypatch.setattr(runtime, "_runtime_now", lambda: datetime.fromisoformat("2026-10-22T09:00:00+00:00"))
    result = get_portfolio_runtime_inspection(db, p.id)
    assert result["permissions"]["allow_new_buys"] is False
    assert result["permissions"]["allow_auto_recovery"] is False
    assert result["permissions"]["allow_risk_exits"] is True
    assert any("2026-10-23" in reason and "尚未到" in reason for reason in result["capability_reasons"]["allow_new_buys"])
    assert result["recovery_summary"]["phase"] == "WAITING_COVERAGE_START"
    assert result["recovery_summary"]["recommended_action"] == "WAIT_COVERAGE_START"
    assert result["recovery_summary"]["can_confirm"] is False
    assert result["recovery_summary"]["waiting_reason"]
    monkeypatch.setattr(runtime, "_runtime_now", lambda: datetime.fromisoformat("2026-10-23T01:00:00+00:00"))
    from app.services.portfolio_reconciliation_events import acknowledge_baseline_sources
    acknowledge_baseline_sources(db, p.id, active["baseline_id"], active["generation"])
    db.flush()  # The mocked accepted baseline includes the pre-existing funding.
    started = get_portfolio_runtime_inspection(db, p.id)
    assert started["permissions"]["allow_new_buys"] is True
    assert started["recovery_summary"]["phase"] == "NO_REVIEW_REQUIRED"
    assert started["recovery_summary"]["recommended_action"] == "NONE"
    assert started["recovery_summary"]["blockers"] == []


def test_legitimate_execution_wait_exposes_wait_instead_of_manual_recovery(account, monkeypatch):
    db, p = account
    seed_run(db, p)
    import app.services.portfolio_inspection_runtime as runtime
    monkeypatch.setattr(runtime, "latest_reconciliation_results", lambda *a, **kw: {
        DAY: {"status": "BLOCKED", "work_state": "waiting_execution", "verdict": "NOT_DUE",
              "next_check_at": "2026-08-21T01:30:00+00:00", "report_id": "actual-wait-report", "report_version": "current"}})
    result = get_portfolio_runtime_inspection(db, p.id)
    summary = result["recovery_summary"]
    assert summary["phase"] == "WAITING_EXECUTION"
    assert summary["recommended_action"] == "WAIT_EXECUTION"
    assert summary["can_confirm"] is False
    assert summary["next_check_at"] == "2026-08-21T01:30:00+00:00"
    assert "等待合法执行" in summary["waiting_reason"]


def test_raw_calendar_rows_do_not_claim_known_reconciliation_dates(account):
    db, p = account
    p.last_reconciled_trade_date = DAY - timedelta(days=1)
    db.add(MarketCalendar(trade_date=DAY, is_trading_day=1))
    db.flush()
    queue = get_reconciliation_queue(db, p.id, today=DAY)
    assert queue["calendar_status"] == "UNKNOWN"
    assert queue["days"] == []
    assert "CALENDAR_UNKNOWN" in queue["limitations"]


def test_historical_resume_without_a_current_immutable_plan_cannot_dispatch_or_write_account(account, monkeypatch):
    from app.services import portfolio_resume_service as resume
    from app.models.async_task import AsyncTaskRecord
    db, p = account
    before = {table: db.scalar(text(f"SELECT COUNT(*) FROM {table}")) for table in ("sim_orders", "sim_trades", "cash_ledger", "positions", "async_tasks")}
    plan = {"portfolio_id": p.id, "resumption_correlation_id": "deadbeef", "today": DAY.isoformat(),
        "pending_days": [{"date": DAY.isoformat(), "idempotency_key": "historical-no-execution", "decision_done": False}]}
    dispatched = []
    import app.services.portfolio_execution_task as execution
    monkeypatch.setattr(execution, "dispatch_execution_tasks", lambda ids: dispatched.extend(ids))
    assert resume.create_resume_tasks_from_plan(db, plan) == []
    assert dispatched == []
    assert {table: db.scalar(text(f"SELECT COUNT(*) FROM {table}")) for table in before} == before
    result = resume.review_resume_execution_date(db, p.id, DAY)
    assert result["enabled"] is False
    assert result["disabled_code"] == "HISTORICAL_EXECUTION_NOT_AUTHORIZED"
    assert result["next_action"]["target"] == "governance:reconciliation"


def test_runtime_keeps_existing_any_healthy_symbol_aggregation_and_restricts_the_bad_symbol(account, monkeypatch):
    db, p = account
    from app.services import portfolio_inspection_diagnostics as diagnostics
    monkeypatch.setattr(diagnostics, "build_portfolio_inspection", lambda *a, **kw: {
        "snapshot_id": "two-real-scopes", "evidence": [], "symbols": [
            {"symbol_id": 1, "market": "SH", "entry_authorized": True, "evidence": []},
            {"symbol_id": 2, "market": "SH", "entry_authorized": True, "evidence": [{
                "evidence_id": "2:bar", "check_code": "DAILY_BAR_FRESHNESS", "status": "MISSING",
                "reason": "第二只标的缺日K", "admission_relevant": True, "affected_capabilities": ["new_buy"]}]}]})
    import app.services.portfolio_inspection_runtime as runtime
    # This test isolates data admission; the binding has its own real suite.
    monkeypatch.setattr(runtime, "_current_binding_check", lambda *a, **kw: {"passed": True, "reason": "当前绑定已确认"})
    state = get_portfolio_runtime_inspection(db, p.id)
    assert state["permissions"]["allow_new_buys"] is True
    evidence = next(check for check in state["recovery_summary"]["checks"] if check["key"] == "data")
    assert evidence["coverage"] == {"entry_symbols": 2, "healthy_symbols": 1, "restricted_symbols": 1}
    assert evidence["restricted_symbol_ids"] == [2]
    assert evidence["evidence"][0]["evidence_id"] == "2:bar"


def test_missing_run_exposes_real_activity_and_an_honestly_disabled_historical_resolution(account):
    db, p = account
    manual = SimOrder(portfolio_id=p.id, symbol_id=1, side="buy", quantity=100,
        submitted_price=10, status="pending", filled_quantity=0, filled_price=0, filled_amount=0, fee=0,
        source_type="manual", created_at=datetime(2026, 8, 20, 2))
    db.add(manual)
    db.flush()
    report = reconcile_trade_date(db, p.id, DAY, persist=False)
    assert report.verdict == "UNVERIFIED_ACTIVITY"
    evidence = {row["source_code"]: row for row in report.activity_evidence}
    assert evidence["sim_orders"]["query_status"] == "QUERIED"
    assert evidence["sim_orders"]["count"] == 1
    assert evidence["sim_orders"]["source_record_refs"] == [f"sim_order:{manual.id}"]
    assert evidence["sim_trades"]["query_status"] == "QUERIED" and evidence["sim_trades"]["count"] == 0
    assert report.execution_expected is None
    assert report.execution_expected_evidence[0]["status"] == "UNKNOWN"
    disabled = next(action for action in report.next_actions if action["action_id"] == "resolve_historical_missed_execution")
    assert disabled["enabled"] is False
    assert disabled["disabled_code"] == "MISSED_EXECUTION_POLICY_NOT_APPROVED"
    assert report.can_confirm is False
    assert len(report.checks) >= 3
    assert p.last_reconciled_trade_date is None


def test_only_a_frozen_unfinished_plan_originally_due_today_can_resume_a_previous_signal_date(account, monkeypatch):
    from app.models.app_setting import AppSetting
    from app.models.decision_engine import DecisionOrderPlanRecord
    from app.services import decision_schedule_gate as schedule
    from app.services.portfolio_resume_service import review_resume_execution_date
    from tests.test_portfolio_inspection import _calendar
    db, p = account
    run = seed_run(db, p)
    evidence = db.get(DecisionEvidence, "inspection-evidence")
    evidence.action, evidence.target_quantity, evidence.target_qty_delta = "BUY", 100, 100
    db.add(DecisionOrderPlanRecord(order_plan_id="original-plan-due-today", decision_run_id=run.id, evidence_id=evidence.id,
        portfolio_id=p.id, symbol_id=1, action="BUY", signal_date=DAY, execution_date=DAY + timedelta(days=1),
        target_quantity=100, direction="buy"))
    _calendar(db)
    source = db.get(AppSetting, "market.cn_trade_calendar")
    provenance = json.loads(source.value_json)
    provenance["refreshed_at"] = "2026-08-01T00:00:00+00:00"
    source.value_json = json.dumps(provenance)
    db.commit()
    monkeypatch.setattr(schedule, "shanghai_now", lambda: datetime.fromisoformat("2026-08-21T02:00:00+00:00"))
    review = review_resume_execution_date(db, p.id, DAY, strategy_snapshot_id=run.strategy_snapshot_id)
    assert review["enabled"] is True and review["mode"] == "CURRENT_ORIGINAL_PLAN"
    assert review["source_refs"][0]["source_record_ref"] == "decision_order_plan:original-plan-due-today"
    assert review_resume_execution_date(db, p.id, DAY, strategy_snapshot_id="a-different-snapshot")["enabled"] is False
    evidence.action = "HOLD"
    db.commit()
    assert review_resume_execution_date(db, p.id, DAY)["enabled"] is False
    evidence.action = "BUY"
    db.commit()
    db.add(SimOrder(portfolio_id=p.id, symbol_id=1, side="buy", quantity=100, submitted_price=10, status="filled",
        filled_quantity=100, filled_price=10, filled_amount=1000, fee=0, decision_evidence_id=evidence.id))
    db.commit()
    assert review_resume_execution_date(db, p.id, DAY)["enabled"] is False


def test_fresh_data_unknown_blocks_recovery_despite_persisted_green(account, monkeypatch):
    db, p = account
    _set_status(db, p, "ADMIN_PAUSED")
    import app.services.portfolio_inspection_diagnostics as diagnostics
    monkeypatch.setattr(diagnostics, "build_portfolio_inspection", lambda *a, **kw: {
        "snapshot_id": "current-unknown", "symbols": [], "evidence": [{
            "check_code": "SCOPE_READ", "status": "CALENDAR_UNKNOWN", "admission_relevant": True,
            "reason": "当前交易所日历尚未核验", "affected_capabilities": ["new_buy"]}]})
    runtime = get_portfolio_runtime_inspection(db, p.id)
    assert runtime["recovery_summary"]["can_confirm"] is False
    check = next(c for c in runtime["recovery_summary"]["checks"] if c["key"] == "data")
    assert check["passed"] is None
    assert check["snapshot_id"] == "current-unknown"
    with pytest.raises(StateTransitionError, match="RECOVERY_PREREQUISITES_NOT_MET"):
        transition_portfolio_state(db, p.id, "READY", review_note="已核实当前范围和运行条件，批准恢复")
    assert _get_status(db, p) == "ADMIN_PAUSED"


def test_holding_only_gap_does_not_expand_into_all_symbol_admission(account, monkeypatch):
    db, p = account
    import app.services.portfolio_inspection_diagnostics as diagnostics
    evidence = {"check_code": "SCORE_FRESHNESS", "status": "STALE", "admission_relevant": True,
        "reason": "仅持仓评分过期", "affected_capabilities": []}
    monkeypatch.setattr(diagnostics, "build_portfolio_inspection", lambda *a, **kw: {
        "snapshot_id": "holding-only", "symbols": [{"evidence": [evidence]}], "evidence": []})
    assert get_portfolio_runtime_inspection(db, p.id)["permissions"]["allow_new_buys"] is True
    evidence["affected_capabilities"] = ["new_buy", "automatic_recovery"]
    assert get_portfolio_runtime_inspection(db, p.id)["permissions"]["allow_new_buys"] is False


def test_reconciliation_confirmation_retains_runtime_lock_for_current_data_failure(account, monkeypatch):
    db, p = account
    seed_run(db, p)
    block(db, p)
    import app.services.portfolio_inspection_diagnostics as diagnostics
    monkeypatch.setattr(diagnostics, "build_portfolio_inspection", lambda *a, **kw: {
        "snapshot_id": "stale-current", "symbols": [], "evidence": [{
            "check_code": "SCORE_FRESHNESS", "status": "STALE", "reason": "当前评分已过期", "admission_relevant": True}]})
    result = confirm_reconciliation_fixed(db, p.id, DAY, operator_id="reviewer",
        acknowledge_all_diffs_cleared=True, review_note="本日执行证据已核实，另保留当前数据限制")
    assert p.status_reconciliation == "READY"
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED"
    assert not result.portfolio_now_ready
    assert json.loads(events(db)[-1].attributes_json)["runtime_recovery_allowed"] is False


def test_confirmation_and_state_audits_are_atomic_on_audit_failure(account, monkeypatch):
    db, p = account
    seed_run(db, p)
    block(db, p)
    db.commit()
    import app.services.data_governance_audit as audit
    original = audit.write_audit_event
    def fail_confirmation(*args, **kwargs):
        if kwargs.get("attributes", {}).get("confirmation_type") == "RECONCILIATION_CONFIRMED":
            raise RuntimeError("audit unavailable")
        return original(*args, **kwargs)
    monkeypatch.setattr(audit, "write_audit_event", fail_confirmation)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        confirm_reconciliation_fixed(db, p.id, DAY, operator_id="reviewer",
            acknowledge_all_diffs_cleared=True)
    db.expire_all()
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED"
    assert p.status_reconciliation == "RECONCILIATION_BLOCKED"
    assert p.last_reconciled_trade_date is None
    assert not events(db)


def accept_current_baseline(db, p):
    from app.models.app_setting import AppSetting
    from app.schemas.portfolio_reconciliation_coverage import CoverageAcceptRequest, CoveragePreviewRequest, CoverageHistoryGap
    from app.services.portfolio_reconciliation_coverage import preview_reconciliation_coverage, accept_reconciliation_coverage
    year_start = date(2026, 1, 1)
    weekdays = [year_start + timedelta(days=i) for i in range(365) if (year_start + timedelta(days=i)).weekday() < 5]
    closed = set(weekdays[:21])  # Isolated adapter fixture, never production calendar data.
    count = 0
    for i in range(365):
        day = year_start + timedelta(days=i)
        opened = int(day.weekday() < 5 and day not in closed)
        db.add(MarketCalendar(trade_date=day, is_trading_day=opened))
        count += opened
    db.add(AppSetting(key="market.cn_trade_calendar", updated_by="exchange-calendar-sync",
        value_json=json.dumps({"source": "https://www.sse.com.cn/disclosure/dealinstruc/closed/", "year": 2026,
            "refreshed_at": "2026-08-18T00:00:00+00:00", "trading_days": count})))
    db.flush()
    proposal = CoveragePreviewRequest(coverage_start_trade_date=DAY, history_gaps=[CoverageHistoryGap(
        trade_date_from=date(2026, 8, 1), trade_date_to=date(2026, 8, 18),
        reason="历史执行证据经多来源检索仍无法恢复", attempted_sources=["历史备份"], evidence_refs=["backup:missing"])])
    now = datetime(2026, 8, 19, 9)
    preview = preview_reconciliation_coverage(db, p.id, proposal, now=now)
    assert preview.can_accept, preview.blockers
    accepted = accept_reconciliation_coverage(db, p.id, CoverageAcceptRequest(**proposal.model_dump(),
        expected_evidence_fingerprint=preview.evidence_fingerprint, expected_cursor_row_version=preview.cursor_row_version,
        acceptance_key="core-current-baseline", review_note="本人核实当前独立账户并接受新覆盖范围", acknowledge_history_unknown=True),
        operator_id="reviewer", now=now)
    db.commit()
    return accepted


def test_accepted_baseline_replay_advances_only_real_current_generation(account):
    db, p = account
    old_checkpoint = date(2026, 8, 10)
    p.last_reconciled_trade_date = old_checkpoint
    accepted = accept_current_baseline(db, p)
    seed_run(db, p)
    report = reconcile_trade_date(db, p.id, DAY)
    assert report.status == "PASSED"
    assert report.cash_audit["source"] == "accepted_baseline_and_post_baseline_journal"
    assert report.progression["current_report_id"]
    assert report.progression["verified_through_trade_date"] == DAY
    assert report.progression["historical_coverage_unknown"] is True
    assert p.last_reconciled_trade_date == old_checkpoint
    version = report.report_version
    assert reconcile_trade_date(db, p.id, DAY, persist=False).report_version == version
    queue = get_reconciliation_queue(db, p.id, today=DAY)
    assert queue["baseline_id"] == accepted.baseline_id
    assert queue["coverage_start"] == DAY.isoformat()
    assert queue["verified_through"] == DAY.isoformat()


def test_old_unknown_report_does_not_reblock_new_coverage_or_move_its_cursor(account):
    db, p = account
    accepted = accept_current_baseline(db, p)
    report = reconcile_trade_date(db, p.id, DAY - timedelta(days=1))
    assert report.verdict == "EVIDENCE_INCOMPLETE"
    assert report.progression["reason"] == "HISTORICAL_EVIDENCE_UNKNOWN"
    assert _get_status(db, p) == "READY"
    assert p.status_reconciliation == "READY"
    from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationCursor
    assert db.get(PortfolioReconciliationCursor, accepted.cursor_id).verified_through_trade_date is None
    assert json.loads(events(db)[-1].attributes_json)["outside_current_coverage"] is True


def test_accepted_snapshot_can_replay_new_fill_without_requiring_old_opening_rows(account):
    db, p = account
    accept_current_baseline(db, p)
    seed_run(db, p)
    evidence = db.get(DecisionEvidence, "inspection-evidence")
    evidence.action, evidence.target_quantity, evidence.target_qty_delta = "BUY", 100, 100
    db.add(SimOrder(id=1, portfolio_id=p.id, symbol_id=1, side="buy", quantity=100,
        submitted_price=10, status="filled", filled_quantity=100, filled_price=10,
        filled_amount=1000, fee=0, decision_evidence_id=evidence.id, created_at=datetime(2026, 8, 21, 1, 30)))
    db.add(SimTrade(id=1, portfolio_id=p.id, symbol_id=1, order_id=1, side="buy",
        quantity=100, price=10, amount=1000, fee=0, created_at=datetime(2026, 8, 21, 1, 30)))
    db.add(Position(portfolio_id=p.id, symbol_id=1, asset_type="stock", quantity=100, avg_cost=10))
    db.add(CashLedger(id=2, portfolio_id=p.id, entry_type="buy", amount=-1000, balance_after=99000,
        ref_type="sim_order", ref_id=1, created_at=datetime(2026, 8, 21, 1, 30)))
    db.delete(db.get(CashLedger, 1))  # Archived pre-baseline book; immutable opening snapshot remains.
    db.flush()
    report = reconcile_trade_date(db, p.id, DAY)
    assert report.status == "PASSED", report.differences
    assert report.expected_cash == report.actual_cash == 99000
    position = db.scalar(select(Position).where(Position.portfolio_id == p.id))
    position.avg_cost = 11
    db.flush()
    changed = reconcile_trade_date(db, p.id, DAY, persist=False)
    assert changed.verdict == "DIFF_FOUND"
    assert any(diff.kind == "POSITION_MISMATCH" for diff in changed.differences)


def test_baseline_replay_uses_actual_account_per_fill_cost_rounding(account):
    db, p = account
    accept_current_baseline(db, p)
    seed_run(db, p)
    evidence = db.get(DecisionEvidence, "inspection-evidence")
    evidence.action, evidence.target_quantity, evidence.target_qty_delta = "BUY", 800, 800
    balance = 100000
    for oid, (qty, price) in enumerate([(100, 10), (500, 9.9), (100, 10.8), (100, 8.09)], start=1):
        stamp = datetime(2026, 8, 21, 1, 30) + timedelta(minutes=oid)
        amount = round(qty * price, 2)
        balance = round(balance - amount, 2)
        db.add(SimOrder(id=oid, portfolio_id=p.id, symbol_id=1, side="buy", quantity=qty,
            submitted_price=price, status="filled", filled_quantity=qty, filled_price=price,
            filled_amount=amount, fee=0, decision_evidence_id=evidence.id, created_at=stamp))
        db.add(SimTrade(id=oid, portfolio_id=p.id, symbol_id=1, order_id=oid, side="buy",
            quantity=qty, price=price, amount=amount, fee=0, created_at=stamp))
        db.add(CashLedger(id=oid + 1, portfolio_id=p.id, entry_type="buy", amount=-amount,
            balance_after=balance, ref_type="sim_order", ref_id=oid, created_at=stamp))
    db.add(Position(portfolio_id=p.id, symbol_id=1, asset_type="stock", quantity=800, avg_cost=9.81))
    db.flush()
    report = reconcile_trade_date(db, p.id, DAY)
    assert report.status == "PASSED", report.differences
    assert report.expected_cash == report.actual_cash == balance


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"])
def test_formal_execution_receipt_is_committed_even_when_not_successful(account, status):
    db, p = account
    run = seed_run(db, p, status=status)
    db.commit()
    from app.models.portfolio_inspection_workflow import ReconciliationSourceEvent
    receipt = db.scalar(select(ReconciliationSourceEvent).where(ReconciliationSourceEvent.portfolio_id == p.id,
        ReconciliationSourceEvent.source_table == "decision_runs", ReconciliationSourceEvent.source_key == run.id))
    assert receipt and json.loads(receipt.facts_json)["after"]["status"] == status
    assert db.scalar(select(SimOrder.id)) is None
