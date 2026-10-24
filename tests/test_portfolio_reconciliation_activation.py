"""First accepted coverage can start without inventing an executed-day pass."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.app_setting import AppSetting
from app.models.decision_engine import DecisionRun, StrategyExecutionSnapshot
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio
from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationCursor, PortfolioReconciliationDay, PortfolioReconciliationReport
from app.models.sim_account import CashLedger
from app.schemas.portfolio_reconciliation_coverage import CoverageAcceptRequest, CoverageHistoryGap, CoveragePreviewRequest
from app.services import portfolio_inspection_runtime as runtime
from app.services import portfolio_reconciliation_activation as service
from app.services import portfolio_reconciliation_coverage as coverage
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.portfolio_state_machine import _get_status, _set_status


pytestmark = pytest.mark.whitebox
NOW = datetime(2026, 10, 22, 9)
START = date(2026, 10, 23)


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE portfolios ADD COLUMN portfolio_status VARCHAR(32)"))
    with Session(engine, expire_on_commit=False) as db:
        p = Portfolio(id=1, name="activation", account_type="simulated", total_capital=10000,
            investable_ratio=.95, cash_reserve_ratio=.05, last_reconciled_trade_date=date(2026, 9, 1),
            status_reconciliation="RECONCILIATION_BLOCKED")
        db.add(p)
        db.flush()
        _set_status(db, p, "RECONCILIATION_BLOCKED")
        db.add(CashLedger(portfolio_id=1, entry_type="deposit", amount=10000, balance_after=10000,
            ref_type="portfolio", ref_id=1, created_at=NOW - timedelta(days=10)))
        # Complete authoritative-adapter fixture with explicit annual closures.
        # This is test evidence and is never used to seed production calendars.
        year_start = date(2026, 1, 1)
        weekdays = [year_start + timedelta(days=i) for i in range(365)
                    if (year_start + timedelta(days=i)).weekday() < 5]
        closed = set(weekdays[:21])
        open_count = 0
        for i in range(365):
            day = year_start + timedelta(days=i)
            is_open = int(day.weekday() < 5 and day not in closed)
            open_count += is_open
            db.add(MarketCalendar(trade_date=day, is_trading_day=is_open))
        db.add(AppSetting(key="market.cn_trade_calendar", updated_by="exchange-calendar-sync",
            value_json=json.dumps({"source": "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
                "year": 2026, "refreshed_at": "2026-10-21T00:00:00+00:00", "trading_days": open_count})))
        db.commit()
        proposal = CoveragePreviewRequest(coverage_start_trade_date=START, history_gaps=[CoverageHistoryGap(
            trade_date_from=date(2026, 10, 1), trade_date_to=date(2026, 10, 21),
            reason="历史证据经过多来源检索仍无法恢复", attempted_sources=["历史备份"], evidence_refs=["backup:missing"])])
        preview = coverage.preview_reconciliation_coverage(db, 1, proposal, now=NOW)
        assert preview.can_accept, preview.blockers
        accepted = coverage.accept_reconciliation_coverage(db, 1, CoverageAcceptRequest(**proposal.model_dump(),
            expected_evidence_fingerprint=preview.evidence_fingerprint,
            expected_cursor_row_version=preview.cursor_row_version, acceptance_key="accepted-current-account",
            review_note="本人逐项核验当前账户并接受新覆盖范围", acknowledge_history_unknown=True),
            operator_id="reviewer-1", now=NOW)
        db.commit()
        monkeypatch.setattr(service, "_now", lambda: NOW)
        monkeypatch.setattr(runtime, "_current_data_check", lambda *args, **kwargs: {
            "passed": True, "reason": "当前真实业务日已核实", "snapshot_id": "data-v1",
            "evidence": [], "reason_codes": [], "has_admission_scope": True, "binding_evidence": []})
        monkeypatch.setattr(runtime, "_current_binding_check", lambda *args, **kwargs: {
            "passed": True, "reason": "实际应用绑定已核实", "snapshot_id": "binding-v1"})
        yield db, p, accepted
    engine.dispose()


def request(review, **kwargs):
    return {"baseline_id": review["baseline_id"], "generation": review["generation"],
        "expected_cursor_row_version": review["cursor_row_version"],
        "expected_evidence_fingerprint": review["evidence_fingerprint"],
        "activation_key": "activate-current-generation", "acknowledge_new_scope": True,
        "review_note": "本人已核验账户及全部门禁并启用新范围", "operator_id": "reviewer-1", **kwargs}


def activation_events(db):
    return [event for event in db.scalars(select(DataGovernanceAuditEvent))
            if json.loads(event.attributes_json or "{}").get("confirmation_type") == service.ACTIVATION_TYPE]


@pytest.mark.parametrize("state,after", [("RECONCILIATION_BLOCKED", "READY"),
    ("ADMIN_PAUSED", "ADMIN_PAUSED"), ("PENDING_INITIAL_REVIEW", "PENDING_INITIAL_REVIEW"),
    ("INTERRUPTED", "INTERRUPTED")])
def test_activation_is_scoped_audit_without_any_historical_pass_or_cursor_progress(store, state, after):
    db, p, accepted = store
    _set_status(db, p, state); db.commit()
    review = service.get_coverage_activation_review(db, 1)
    assert review["can_confirm"], review["blockers"]
    first = service.confirm_initial_coverage_review(db, 1, **request(review))
    db.commit()
    assert first["current_state"] == after and _get_status(db, p) == after
    assert p.status_reconciliation == "READY"
    assert first["reconciliation_passed"] is False and first["historical_coverage_unknown"] is True
    assert first["scope_authorized"] is True and first["awaiting_coverage_start"] is True
    assert first["trading_resumed"] is False
    cursor = db.get(PortfolioReconciliationCursor, accepted.cursor_id)
    assert cursor.verified_through_trade_date is None and cursor.row_version == 1
    assert p.last_reconciled_trade_date == date(2026, 9, 1)
    assert db.scalar(select(func.count()).select_from(PortfolioReconciliationDay)) == 0
    assert db.scalar(select(func.count()).select_from(PortfolioReconciliationReport)) == 0
    event = activation_events(db)[0]
    attrs = json.loads(event.attributes_json)
    assert attrs["baseline_id"] == accepted.baseline_id and attrs["generation"] == 1
    assert attrs["ack"] is True and attrs["source_verified"] is True
    assert attrs["history_gaps"][0]["verdict"] == "HISTORICAL_EVIDENCE_UNKNOWN"
    assert json.loads(event.before_json)["current_state"] == state
    assert json.loads(event.after_json)["current_state"] == after


def test_repeat_same_key_returns_original_result_and_key_reuse_is_conflict(store):
    db, p, accepted = store
    review = service.get_coverage_activation_review(db, 1)
    payload = request(review)
    result = service.confirm_initial_coverage_review(db, 1, **payload); db.commit()
    again = service.confirm_initial_coverage_review(db, 1, **payload)
    assert again["idempotent"] and again["audit_event_id"] == result["audit_event_id"]
    assert len(activation_events(db)) == 1
    with pytest.raises(coverage.CoverageError) as error:
        service.confirm_initial_coverage_review(db, 1, **{**payload, "review_note": "不同的审核意见不能复用原先启用幂等键"})
    assert error.value.code == "ACTIVATION_KEY_REUSED" and error.value.status_code == 409
    after = service.get_coverage_activation_review(db, 1)
    assert after["already_activated"] and not after["can_confirm"]


def test_preview_fingerprint_is_stable_across_read_time_and_today_start_is_usable(store, monkeypatch):
    db, p, accepted = store
    before = service.get_coverage_activation_review(db, 1)
    monkeypatch.setattr(service, "_now", lambda: datetime(2026, 10, 23, 1))
    after = service.get_coverage_activation_review(db, 1)
    assert after["can_confirm"] and before["evidence_fingerprint"] == after["evidence_fingerprint"]
    service.confirm_initial_coverage_review(db, 1, **request(before))


@pytest.mark.parametrize("change,code", [("source", "ACCOUNT_EVIDENCE_CHANGED"),
    ("data", "CURRENT_DATA_NOT_READY"), ("binding", "CURRENT_BINDING_NOT_READY"),
    ("dimension", "CURRENT_DATA_NOT_READY"), ("model", "CURRENT_BINDING_NOT_READY"),
    ("calendar", "COVERAGE_CALENDAR_CHANGED"), ("late", "COVERAGE_START_MISSED")])
def test_every_current_gate_still_blocks_initial_activation(store, monkeypatch, change, code):
    db, p, accepted = store
    before = service.get_coverage_activation_review(db, 1)
    if change == "source":
        db.add(CashLedger(portfolio_id=1, entry_type="deposit", amount=100, balance_after=10100,
            ref_type="portfolio", ref_id=1, created_at=NOW)); db.commit()
    if change == "data":
        monkeypatch.setattr(runtime, "_current_data_check", lambda *args, **kwargs: {
            "passed": False, "reason": "评分未到最新业务日", "snapshot_id": "data-v2",
            "evidence": [], "reason_codes": ["SCORE_FRESHNESS"], "has_admission_scope": True, "binding_evidence": []})
    if change == "binding":
        monkeypatch.setattr(runtime, "_current_binding_check", lambda *args, **kwargs: {
            "passed": False, "reason": "已应用策略绑定发生变化", "snapshot_id": "binding-v2"})
    if change == "dimension": p.status_score = "SCORE_STALE"; db.commit()
    if change == "model": p.status_model = "MODEL_INACTIVE"; db.commit()
    if change == "calendar": db.get(MarketCalendar, START).is_trading_day = 0; db.commit()
    if change == "late": monkeypatch.setattr(service, "_now", lambda: datetime(2026, 10, 24, 9))
    preview = service.get_coverage_activation_review(db, 1)
    assert not preview["can_confirm"] and code in {b["code"] for b in preview["blockers"]}
    with pytest.raises(coverage.CoverageError) as error:
        service.confirm_initial_coverage_review(db, 1, **request(before))
    assert error.value.status_code == 409
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED" and p.status_reconciliation == "RECONCILIATION_BLOCKED"
    assert len(activation_events(db)) == 0


@pytest.mark.parametrize("field,value,code", [("generation", 99, "COVERAGE_GENERATION_CHANGED"),
    ("expected_cursor_row_version", 99, "COVERAGE_VERSION_CHANGED"),
    ("expected_evidence_fingerprint", "a" * 64, "ACTIVATION_EVIDENCE_CHANGED"),
    ("review_note", "理由太短", "REVIEW_NOTE_REQUIRED"),
    ("acknowledge_new_scope", False, "NEW_SCOPE_ACK_REQUIRED"),
    ("operator_id", "cron_worker", "PERSONAL_REVIEW_REQUIRED")])
def test_confirmation_requires_exact_version_and_personal_explicit_decision(store, field, value, code):
    db, _, _ = store
    review = service.get_coverage_activation_review(db, 1)
    with pytest.raises(coverage.CoverageError) as error:
        service.confirm_initial_coverage_review(db, 1, **request(review, **{field: value}))
    assert error.value.code == code and len(activation_events(db)) == 0


def test_audit_failure_rolls_back_runtime_and_dimension_without_outer_rollback(store, monkeypatch):
    db, p, accepted = store
    review = service.get_coverage_activation_review(db, 1)
    original = service.write_audit_event
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("audit insertion failed")
    monkeypatch.setattr(service, "write_audit_event", fail)
    with pytest.raises(RuntimeError, match="audit insertion failed"):
        service.confirm_initial_coverage_review(db, 1, **request(review))
    db.commit()
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED"
    assert db.get(Portfolio, 1).status_reconciliation == "RECONCILIATION_BLOCKED"
    assert len(activation_events(db)) == 0


def test_execution_guard_blocks_dates_before_start_and_requires_same_generation_activation(store):
    db, p, accepted = store
    with pytest.raises(coverage.CoverageError) as error:
        service.assert_execution_within_active_coverage(db, 1, START)
    assert error.value.code == "INITIAL_COVERAGE_REVIEW_REQUIRED"
    review = service.get_coverage_activation_review(db, 1)
    service.confirm_initial_coverage_review(db, 1, **request(review)); db.commit()
    service.assert_execution_within_active_coverage(db, 1, START)
    with pytest.raises(coverage.CoverageError) as error:
        service.assert_execution_within_active_coverage(db, 1, START - timedelta(days=1))
    assert error.value.code == "OUTSIDE_CURRENT_COVERAGE"
    active = coverage.get_active_coverage(db, 1)
    assert service.get_initial_coverage_activation(db, 1, active={**active, "generation": 2}) is None


def test_activated_fact_survives_later_real_account_source_changes(store):
    db, p, accepted = store
    review = service.get_coverage_activation_review(db, 1)
    result = service.confirm_initial_coverage_review(db, 1, **request(review)); db.commit()
    db.add(CashLedger(portfolio_id=1, entry_type="deposit", amount=100, balance_after=10100,
        ref_type="portfolio", ref_id=1, created_at=NOW)); db.commit()
    activation = service.get_initial_coverage_activation(db, 1)
    assert activation["audit_id"] == result["audit_event_id"]
    assert service.get_coverage_activation_review(db, 1)["already_activated"]
    service.assert_execution_within_active_coverage(db, 1, START)


def test_old_activation_cannot_cover_new_generation_and_no_baseline_is_explicit_unknown(store):
    db, p, accepted = store
    active = coverage.get_active_coverage(db, 1)
    assert service.get_initial_coverage_activation(db, 1, active={**active, "baseline_id": "different-baseline"}) is None
    db.add(Portfolio(id=2, name="legacy", account_type="simulated", total_capital=1000,
        investable_ratio=.95, cash_reserve_ratio=.05)); db.commit()
    review = service.get_coverage_activation_review(db, 2)
    assert not review["can_confirm"] and review["evidence_fingerprint"] is None
    assert review["blockers"][0]["code"] == "ACCEPTED_COVERAGE_REQUIRED"
    service.assert_execution_within_active_coverage(db, 2, date(2026, 1, 1))


def test_already_created_execution_requires_real_reconciliation_instead_of_activation(store):
    db, p, accepted = store
    db.add(StrategyExecutionSnapshot(id="applied", snapshot_no=1, portfolio_id=1,
        decision_clock_json="{}", member_snapshot_json="[]", snapshot_hash="a" * 64,
        effective_from=NOW - timedelta(days=1)))
    db.flush()
    db.add(DecisionRun(id="first-run", strategy_snapshot_id="applied", portfolio_id=1,
        run_type="auto_simulation", status="SUCCEEDED", trade_date=START,
        decision_at=NOW, data_cutoff_at=NOW, execution_at=NOW, created_at=NOW))
    db.commit()
    review = service.get_coverage_activation_review(db, 1)
    assert not review["can_confirm"]
    assert "COVERAGE_EXECUTION_ALREADY_STARTED" in {b["code"] for b in review["blockers"]}


def test_real_dual_trade_cannot_disable_scope_guard_with_readiness_flag_and_dry_run_can_inspect_old_day(store, monkeypatch):
    from app.services import auto_trade_dual_run as dual
    db, p, accepted = store
    calls = []
    def inner(*args, **kwargs):
        calls.append(kwargs)
        return {"dry_run": kwargs["dry_run"], "trade_date": kwargs["trade_date"].isoformat()}
    monkeypatch.setattr(dual, "_run_dual_trade_inner", inner)
    for day, code in [(START, "INITIAL_COVERAGE_REVIEW_REQUIRED"),
                      (START - timedelta(days=1), "OUTSIDE_CURRENT_COVERAGE")]:
        with pytest.raises(coverage.CoverageError) as error:
            dual.run_dual_trade(db, portfolio_id=1, dry_run=False, require_readiness=False,
                                save_diff=False, trade_date=day)
        assert error.value.code == code
    assert calls == []  # Guard runs before any order-producing inner path.
    original_audits = db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent))
    preview = dual.run_dual_trade(db, portfolio_id=1, dry_run=True, require_readiness=False,
        save_diff=False, trade_date=START - timedelta(days=1))
    assert preview["dry_run"] is True and len(calls) == 1
    assert db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == original_audits
    review = service.get_coverage_activation_review(db, 1)
    service.confirm_initial_coverage_review(db, 1, **request(review)); db.commit()
    calls.clear()
    with pytest.raises(coverage.CoverageError) as error:
        dual.run_dual_trade(db, portfolio_id=1, dry_run=False, require_readiness=False,
            save_diff=False, trade_date=START - timedelta(days=1))
    assert error.value.code == "OUTSIDE_CURRENT_COVERAGE" and calls == []
    actual = dual.run_dual_trade(db, portfolio_id=1, dry_run=False, require_readiness=False,
        save_diff=False, trade_date=START)
    assert actual["dry_run"] is False and len(calls) == 1


def test_confirmation_requests_current_reads_for_data_and_actual_binding(store, monkeypatch):
    db, p, accepted = store
    review = service.get_coverage_activation_review(db, 1)
    called = []
    old_data, old_binding = runtime._current_data_check, runtime._current_binding_check
    def data(*args, **kwargs):
        called.append(("data", kwargs.get("current_read")))
        return old_data(*args, **kwargs)
    def binding(*args, **kwargs):
        called.append(("binding", kwargs.get("current_read")))
        return old_binding(*args, **kwargs)
    monkeypatch.setattr(runtime, "_current_data_check", data)
    monkeypatch.setattr(runtime, "_current_binding_check", binding)
    service.confirm_initial_coverage_review(db, 1, **request(review))
    assert called == [("data", True), ("binding", True)]
