"""Current-account coverage keeps old unknowns and accepts only real sources."""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.market_data import MarketCalendar
from app.models.app_setting import AppSetting
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_reconciliation_coverage import (
    PortfolioAccountBaseline, PortfolioReconciliationCursor, PortfolioReconciliationDay,
    PortfolioReconciliationReport,
)
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.schemas.portfolio_reconciliation_coverage import CoverageAcceptRequest, CoverageHistoryGap, CoveragePreviewRequest
from app.services import portfolio_reconciliation_coverage as service
from app.services.data_governance_audit import DataGovernanceAuditEvent

pytestmark = pytest.mark.whitebox
NOW = datetime(2026, 10, 22, 9)


@pytest.fixture
def store():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Portfolio(id=1, name="coverage", account_type="simulated", total_capital=10000,
            investable_ratio=.95, cash_reserve_ratio=.05, last_reconciled_trade_date=date(2026, 9, 1), status_model="MODEL_INACTIVE"))
        db.add(Symbol(id=1, symbol="000001", name="平安银行", market="CN", asset_type="stock"))
        db.flush()
        db.add_all([
            CashLedger(id=1, portfolio_id=1, entry_type="deposit", amount=10000, balance_after=10000, ref_type="portfolio", ref_id=1, created_at=NOW - timedelta(days=10)),
            CashLedger(id=2, portfolio_id=1, entry_type="buy", amount=-1001, balance_after=8999, ref_type="sim_order", ref_id=1, created_at=NOW - timedelta(days=9)),
            SimOrder(id=1, portfolio_id=1, symbol_id=1, side="buy", quantity=100, submitted_price=10,
                     status="filled", filled_quantity=100, filled_price=10, filled_amount=1000, fee=1, created_at=NOW - timedelta(days=9)),
            SimTrade(id=1, portfolio_id=1, symbol_id=1, order_id=1, side="buy", quantity=100, price=10, amount=1000, fee=1, created_at=NOW - timedelta(days=9)),
            Position(id=1, portfolio_id=1, symbol_id=1, quantity=100, avg_cost=10, asset_type="stock", updated_at=NOW),
        ])
        # An isolated authoritative-adapter fixture, never a production calendar
        # seed. Include a full year and explicit closures so bare weekday rows
        # cannot satisfy the provenance/coverage guard.
        year_start = date(2026, 1, 1)
        weekdays = [year_start + timedelta(days=i) for i in range(365) if (year_start + timedelta(days=i)).weekday() < 5]
        closed = set(weekdays[:21])
        open_count = 0
        for offset in range(365):
            day = year_start + timedelta(days=offset)
            is_open = int(day.weekday() < 5 and day not in closed)
            open_count += is_open
            db.add(MarketCalendar(trade_date=day, is_trading_day=is_open))
        db.add(AppSetting(key="market.cn_trade_calendar", updated_by="exchange-calendar-sync", value_json=json.dumps({
            "source": "https://www.sse.com.cn/disclosure/dealinstruc/closed/", "year": 2026,
            "refreshed_at": "2026-10-21T00:00:00+00:00", "trading_days": open_count,
        })))
        db.commit()
        yield db
    engine.dispose()


def proposal(**kwargs):
    return CoveragePreviewRequest(coverage_start_trade_date=date(2026, 10, 23), history_gaps=[CoverageHistoryGap(
        trade_date_from=date(2026, 10, 1), trade_date_to=date(2026, 10, 21),
        reason="历史配置和旧执行器证据已确认无法恢复", attempted_sources=["历史备份", "配置审计"], evidence_refs=["backup-job:failed-1"],
    )], **kwargs)


def accept_request(preview, proposed=None, *, key="coverage-request-1"):
    return CoverageAcceptRequest(**(proposed or proposal()).model_dump(),
        expected_evidence_fingerprint=preview.evidence_fingerprint,
        expected_cursor_row_version=preview.cursor_row_version, acceptance_key=key,
        review_note="本人核验当前逐标的资产并接受新覆盖起点", acknowledge_history_unknown=True)


def test_real_preview_and_accept_preserve_unknown_and_do_not_certify_or_resume(store):
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    assert preview.can_accept, [item.model_dump() for item in preview.blockers]
    assert preview.snapshot["cash"] == "8999.0"
    assert preview.snapshot["positions"][0]["quantity"] == "100.0"
    assert preview.snapshot["positions"][0]["security_id"] == "CN:000001"
    assert preview.source_refs and all("content_hash" in item for item in preview.source_refs)
    assert store.scalar(select(func.count()).select_from(PortfolioAccountBaseline)) == 0
    result = service.accept_reconciliation_coverage(store, 1, accept_request(preview), operator_id="reviewer-1", now=NOW)
    store.commit()
    cursor = store.get(PortfolioReconciliationCursor, result.cursor_id)
    assert cursor.generation == 1 and cursor.verified_through_trade_date is None
    assert result.history_gaps[0]["verdict"] == "HISTORICAL_EVIDENCE_UNKNOWN"
    assert result.history_gaps[0]["closure_type"] == "ARCHIVED_UNVERIFIABLE"
    assert result.trading_resumed is False and result.runtime_enabled is False
    portfolio = store.get(Portfolio, 1)
    assert portfolio.last_reconciled_trade_date == date(2026, 9, 1)
    assert portfolio.status_model == "MODEL_INACTIVE"
    baseline = store.get(PortfolioAccountBaseline, result.baseline_id)
    assert baseline.accepted_audit_id == result.audit_event_id
    assert json.loads(baseline.snapshot_json)["reviewer"] == "reviewer-1"
    assert store.scalar(select(func.count()).select_from(PortfolioReconciliationDay)) == 0
    assert store.scalar(select(func.count()).select_from(PortfolioReconciliationReport)) == 0


@pytest.mark.parametrize("change,code", [
    ("cash", "CASH_LEDGER_MISMATCH"), ("position", "POSITION_MISMATCH"),
    ("pending", "OUTSTANDING_ORDER"), ("orphan", "UNEXPLAINED_TRADE"),
    ("opening", "CASH_OPENING_UNKNOWN"), ("source", "UNVERIFIED_FUNDING_SOURCE"),
    ("future", "ACCOUNT_SOURCE_AFTER_SNAPSHOT"), ("currency", "CURRENCY_SOURCE_UNSUPPORTED"),
    ("overfill", "ORDER_OVERFILL"),
])
def test_rejects_real_account_differences_unknown_origin_and_obligations(store, change, code):
    if change == "cash": store.get(CashLedger, 2).balance_after += 1
    if change == "position": store.get(Position, 1).quantity += 1
    if change == "pending": store.get(SimOrder, 1).status = "pending_retry"
    if change == "orphan": store.get(SimTrade, 1).order_id = 99
    if change == "opening": store.get(CashLedger, 1).entry_type = "adjustment"
    if change == "source": store.get(CashLedger, 1).ref_type = "backtest_run"
    if change == "future": store.get(CashLedger, 1).created_at = NOW + timedelta(days=1)
    if change == "currency": store.get(Portfolio, 1).currency = "USD"
    if change == "overfill": store.get(SimOrder, 1).quantity = 50
    store.commit()
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    assert not preview.can_accept
    assert code in {item.code for item in preview.blockers}
    with pytest.raises(service.CoverageError, match="不能仅凭备注") as error:
        service.accept_reconciliation_coverage(store, 1, accept_request(preview), operator_id="reviewer-1", now=NOW)
    assert error.value.code == "BASELINE_NOT_ACCEPTABLE"
    assert store.scalar(select(func.count()).select_from(PortfolioAccountBaseline)) == 0


def test_fingerprint_detects_changed_sources_even_when_new_sources_are_consistent(store):
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    store.add(CashLedger(portfolio_id=1, entry_type="deposit", amount=100, balance_after=9099,
                         ref_type="portfolio", ref_id=1, created_at=NOW))
    store.commit()
    assert service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW).can_accept
    with pytest.raises(service.CoverageError) as error:
        service.accept_reconciliation_coverage(store, 1, accept_request(preview), operator_id="reviewer-1", now=NOW)
    assert error.value.code == "ACCOUNT_EVIDENCE_CHANGED"


def test_acceptance_idempotency_cannot_reuse_key_for_another_decision(store):
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    request = accept_request(preview)
    first = service.accept_reconciliation_coverage(store, 1, request, operator_id="reviewer-1", now=NOW)
    store.commit()
    second = service.accept_reconciliation_coverage(store, 1, request, operator_id="reviewer-1", now=NOW)
    assert second.idempotent and second.baseline_id == first.baseline_id and second.generation == 1
    assert store.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == 1
    with pytest.raises(service.CoverageError) as error:
        service.accept_reconciliation_coverage(store, 1, request.model_copy(update={"review_note": "不同审核内容不能复用相同幂等键"}), operator_id="reviewer-1", now=NOW)
    assert error.value.code == "ACCEPTANCE_KEY_REUSED"


def test_audit_failure_rolls_back_baseline_cursor_and_success_audit_atomically(store, monkeypatch):
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    original = service.write_audit_event
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("audit failed after flush")
    monkeypatch.setattr(service, "write_audit_event", fail)
    with pytest.raises(RuntimeError, match="audit failed"):
        service.accept_reconciliation_coverage(store, 1, accept_request(preview), operator_id="reviewer-1", now=NOW)
    assert store.scalar(select(func.count()).select_from(PortfolioAccountBaseline)) == 0
    assert store.scalar(select(func.count()).select_from(PortfolioReconciliationCursor)) == 0
    assert store.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == 0


def test_new_generation_keeps_old_baseline_days_reports_and_old_checkpoint(store):
    first_preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    first = service.accept_reconciliation_coverage(store, 1, accept_request(first_preview), operator_id="reviewer-1", now=NOW)
    store.commit()
    store.add(PortfolioReconciliationDay(id="old-day", cursor_id=first.cursor_id, generation=1,
        target_trade_date=date(2026, 10, 23), work_state="needs_review", verdict="EVIDENCE_INCOMPLETE"))
    store.flush()
    store.add(PortfolioReconciliationReport(id="old-report", day_id="old-day", report_version=1,
        evidence_version=1, baseline_id=first.baseline_id, checks_json="{}", findings_json="[]", source_refs_json="[]",
        evidence_hash="a" * 64, verdict="EVIDENCE_INCOMPLETE", policy_version="old-policy", calendar_version="old-calendar", audit_event_id=first.audit_event_id))
    store.commit()
    next_proposal = proposal().model_copy(update={"coverage_start_trade_date": date(2026, 10, 27)})
    next_preview = service.preview_reconciliation_coverage(store, 1, next_proposal, now=datetime(2026, 10, 26, 9))
    assert next_preview.can_accept
    result = service.accept_reconciliation_coverage(store, 1, accept_request(next_preview, next_proposal, key="coverage-request-2"), operator_id="reviewer-1", now=datetime(2026, 10, 26, 9))
    store.commit()
    assert result.generation == 2 and result.verified_through_trade_date is None
    assert store.get(PortfolioAccountBaseline, first.baseline_id)
    assert store.get(PortfolioReconciliationDay, "old-day").generation == 1
    assert store.get(PortfolioReconciliationReport, "old-report").verdict == "EVIDENCE_INCOMPLETE"
    assert store.get(Portfolio, 1).last_reconciled_trade_date == date(2026, 9, 1)
    assert len(service.get_reconciliation_coverage(store, 1)["baselines"]) == 2
    with pytest.raises(service.CoverageError) as stale_receipt:
        service.accept_reconciliation_coverage(store, 1, accept_request(first_preview, key="coverage-request-1"), operator_id="reviewer-1", now=NOW)
    assert stale_receipt.value.code == "COVERAGE_ACCEPTANCE_SUPERSEDED"


def test_calendar_is_not_inferred_from_weekday_and_current_day_cannot_be_backdated(store):
    proposed = proposal().model_copy(update={"coverage_start_trade_date": date(2026, 10, 22)})
    assert not service.preview_reconciliation_coverage(store, 1, proposed, now=NOW).can_accept
    store.execute(text("DELETE FROM trade_calendar WHERE date='2026-10-23'")); store.commit()
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    assert not preview.can_accept
    assert {"CALENDAR_COVERAGE_INCOMPLETE", "CALENDAR_PROVENANCE_REQUIRED"} & {item.code for item in preview.blockers}


def test_missing_source_and_external_account_fail_closed_without_equity_fallback(store):
    store.get(Portfolio, 1).account_type = "real"; store.commit()
    store.execute(text("DROP TABLE cash_ledger")); store.commit()
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    codes = {item.code for item in preview.blockers}
    assert {"ACCOUNT_SOURCE_MISSING", "EXTERNAL_ACCOUNT_EVIDENCE_UNAVAILABLE"} <= codes
    assert preview.snapshot["cash"] is None and not preview.can_accept


def accepted_account(store):
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    result = service.accept_reconciliation_coverage(store, 1, accept_request(preview), operator_id="reviewer-1", now=NOW)
    store.commit()
    return result


def record_report(store, day, *, verdict="PASSED", evidence="a" * 64, generation=1):
    return service.record_coverage_reconciliation(store, portfolio_id=1, trade_date=day, verdict=verdict,
        checks={"cash": {"passed": verdict == "PASSED", "actual": "8999.0", "expected": "8999.0"}},
        findings=[] if verdict == "PASSED" else [{"code": "MISSING_EVIDENCE"}],
        source_refs=[{"source": "cash_ledger", "row_ids": ["1", "2"], "content_hash": "b" * 64}],
        evidence_hash=evidence, expected_generation=generation, now=datetime(2026, 10, 27, 9))


def test_out_of_order_reports_do_not_skip_first_date_and_true_calendar_consumes_continuously(store):
    accepted = accepted_account(store)
    monday = record_report(store, date(2026, 10, 26))
    store.commit()
    assert monday["verified_through_trade_date"] is None
    assert monday["first_blocked_trade_date"] == date(2026, 10, 23)
    assert not monday["can_advance"]
    friday = record_report(store, date(2026, 10, 23), evidence="c" * 64)
    store.commit()
    assert friday["verified_through_trade_date"] == date(2026, 10, 26)
    assert friday["can_advance"] and friday["first_blocked_trade_date"] is None
    assert store.get(Portfolio, 1).last_reconciled_trade_date == date(2026, 9, 1)
    assert service.get_active_coverage(store, 1)["baseline_id"] == accepted.baseline_id
    assert store.scalar(select(func.count()).select_from(PortfolioReconciliationReport)) == 2


def test_report_versions_are_immutable_retries_dedup_and_new_bad_evidence_invalidates_current_coverage(store):
    accepted_account(store)
    first = record_report(store, date(2026, 10, 23)); store.commit()
    repeat = record_report(store, date(2026, 10, 23)); store.commit()
    assert repeat["idempotent"] and repeat["current_report_id"] == first["current_report_id"]
    bad = record_report(store, date(2026, 10, 23), verdict="EVIDENCE_INCOMPLETE", evidence="c" * 64); store.commit()
    assert bad["report_version"] == 2 and bad["verified_through_trade_date"] is None
    assert store.get(PortfolioReconciliationReport, first["current_report_id"]).verdict == "PASSED"
    frozen = store.get(PortfolioReconciliationReport, first["current_report_id"])
    frozen.verdict = "DIFF_FOUND"
    with pytest.raises(ValueError, match="immutable"):
        store.flush()
    store.rollback()


def test_calendar_revision_creates_new_report_and_idempotent_continuity_recalculation_is_persisted(store):
    accepted = accepted_account(store)
    first = record_report(store, date(2026, 10, 23)); store.commit()
    state = store.get(AppSetting, "market.cn_trade_calendar")
    provenance = json.loads(state.value_json)
    provenance["refreshed_at"] = "2026-10-22T01:00:00+00:00"
    provenance["publication_version"] = "calendar-revision-2"
    state.value_json = json.dumps(provenance); store.commit()
    revised = record_report(store, date(2026, 10, 23)); store.commit()
    assert not revised["idempotent"] and revised["report_version"] == 2
    assert revised["current_report_id"] != first["current_report_id"]
    cursor = store.get(PortfolioReconciliationCursor, accepted.cursor_id)
    cursor.verified_through_trade_date = None
    before_version = cursor.row_version
    store.commit()
    repeated = record_report(store, date(2026, 10, 23)); store.commit()
    assert repeated["idempotent"] and repeated["verified_through_trade_date"] == date(2026, 10, 23)
    assert cursor.verified_through_trade_date == date(2026, 10, 23) and cursor.row_version == before_version + 1


def test_generation_boundaries_empty_pass_and_nontrading_account_exception_are_guarded(store):
    accepted_account(store)
    with pytest.raises(service.CoverageError) as error:
        record_report(store, date(2026, 10, 21))
    assert error.value.code == "OUTSIDE_CURRENT_COVERAGE"
    with pytest.raises(service.CoverageError) as error:
        record_report(store, date(2026, 10, 23), generation=99)
    assert error.value.code == "COVERAGE_GENERATION_CHANGED"
    with pytest.raises(service.CoverageError) as error:
        service.record_coverage_reconciliation(store, portfolio_id=1, trade_date=date(2026, 10, 23),
            verdict="PASSED", checks={}, findings=[], source_refs=[], evidence_hash="a" * 64)
    assert error.value.code == "VERIFIED_REPORT_EVIDENCE_REQUIRED"
    record_report(store, date(2026, 10, 23)); store.commit()
    record_report(store, date(2026, 10, 24), verdict="DIFF_FOUND", evidence="d" * 64); store.commit()
    monday = record_report(store, date(2026, 10, 26), evidence="c" * 64); store.commit()
    assert monday["first_blocked_trade_date"] == date(2026, 10, 24)
    assert monday["verified_through_trade_date"] == date(2026, 10, 23)


def test_report_audit_failure_rolls_back_report_day_and_cursor(store, monkeypatch):
    accepted = accepted_account(store)
    before_version = store.get(PortfolioReconciliationCursor, accepted.cursor_id).row_version
    def fail(*args, **kwargs):
        raise RuntimeError("report audit unavailable")
    monkeypatch.setattr(service, "write_audit_event", fail)
    with pytest.raises(RuntimeError, match="report audit"):
        record_report(store, date(2026, 10, 23))
    assert store.scalar(select(func.count()).select_from(PortfolioReconciliationDay)) == 0
    assert store.scalar(select(func.count()).select_from(PortfolioReconciliationReport)) == 0
    assert store.get(PortfolioReconciliationCursor, accepted.cursor_id).row_version == before_version


def test_initial_scope_source_revalidation_does_not_reinterpret_the_accepted_start(store):
    accepted = accepted_account(store)
    current = service.revalidate_active_coverage_sources(store, 1, expected_baseline_id=accepted.baseline_id)
    assert current["source_verified"] and current["coverage_start_trade_date"] == date(2026, 10, 23)
    store.get(Position, 1).quantity += 1; store.commit()
    with pytest.raises(service.CoverageError) as error:
        service.revalidate_active_coverage_sources(store, 1, expected_baseline_id=accepted.baseline_id)
    assert error.value.code == "BASELINE_NOT_ACCEPTABLE"


def test_migration_creates_all_four_tables_without_modifying_account_data():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / "alembic/versions/2026_10_22_0074_wps_0023_074_reconciliation_coverage.py"
    spec = importlib.util.spec_from_file_location("coverage_migration", path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for table in (Portfolio.__table__, DataGovernanceAuditEvent.__table__):
            table.create(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            for model in (PortfolioAccountBaseline, PortfolioReconciliationCursor, PortfolioReconciliationDay, PortfolioReconciliationReport):
                assert connection.scalar(select(func.count()).select_from(model.__table__)) == 0
            migration.downgrade()
    engine.dispose()


def test_later_day_report_cannot_consume_prior_report_with_superseded_calendar(store):
    accepted = accepted_account(store)
    friday = record_report(store, date(2026, 10, 23)); store.commit()
    record_report(store, date(2026, 10, 26), evidence="c" * 64); store.commit()
    assert service.get_active_coverage(store, 1)["verified_through_trade_date"] == date(2026, 10, 26)
    setting = store.get(AppSetting, "market.cn_trade_calendar")
    metadata = json.loads(setting.value_json); metadata["publication_version"] = "corrected-calendar"
    setting.value_json = json.dumps(metadata); store.commit()
    monday = record_report(store, date(2026, 10, 26), evidence="c" * 64); store.commit()
    assert monday["verified_through_trade_date"] is None
    assert monday["first_blocked_trade_date"] == date(2026, 10, 23)
    assert store.get(PortfolioReconciliationReport, friday["current_report_id"]).verdict == "PASSED"
    rerun = record_report(store, date(2026, 10, 23)); store.commit()
    assert rerun["report_version"] == 2 and rerun["verified_through_trade_date"] == date(2026, 10, 26)
    assert store.get(PortfolioReconciliationCursor, accepted.cursor_id).verified_through_trade_date == date(2026, 10, 26)


def test_recalculated_cursor_and_row_version_roll_back_if_recalculation_audit_fails(store, monkeypatch):
    accepted = accepted_account(store)
    record_report(store, date(2026, 10, 23)); store.commit()
    cursor = store.get(PortfolioReconciliationCursor, accepted.cursor_id)
    cursor.verified_through_trade_date = None; store.commit()
    before_version = cursor.row_version
    monkeypatch.setattr(service, "write_audit_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
    with pytest.raises(RuntimeError, match="audit unavailable"):
        record_report(store, date(2026, 10, 23))
    store.expire_all()
    assert cursor.verified_through_trade_date is None and cursor.row_version == before_version
    assert store.scalar(select(func.count()).select_from(PortfolioReconciliationReport)) == 1


def test_record_rejects_stale_cursor_and_explicit_failed_checks(store):
    accepted = accepted_account(store)
    with pytest.raises(service.CoverageError) as exc:
        service.record_coverage_reconciliation(store, portfolio_id=1, trade_date=date(2026, 10, 23),
            verdict="PASSED", checks={"cash": {"passed": False}}, findings=[], source_refs=[{"source": "cash_ledger"}], evidence_hash="a" * 64)
    assert exc.value.code == "FAILED_CHECK_CANNOT_PASS"
    with pytest.raises(service.CoverageError) as exc:
        service.record_coverage_reconciliation(store, portfolio_id=1, trade_date=date(2026, 10, 23),
            verdict="PASSED", checks={"cash": {"passed": True}}, findings=[], source_refs=[{"source": "cash_ledger"}],
            evidence_hash="a" * 64, expected_cursor_row_version=99)
    assert exc.value.code == "COVERAGE_CURSOR_CHANGED"
    assert store.get(PortfolioReconciliationCursor, accepted.cursor_id).verified_through_trade_date is None


def test_read_only_report_material_keeps_exact_old_version_after_recheck(store):
    from app.services.portfolio_inspection_evidence import get_inspection_history_material
    accepted = accepted_account(store)
    baseline_material = get_inspection_history_material(store, 1, source_event_id=accepted.audit_event_id,
        kind="baseline", ref=accepted.baseline_id)
    assert baseline_material.status == "available" and baseline_material.material_kind == "immutable_baseline"
    assert baseline_material.data["history_gaps"][0]["verdict"] == "HISTORICAL_EVIDENCE_UNKNOWN"
    first = record_report(store, date(2026, 10, 23)); store.commit()
    original = store.get(PortfolioReconciliationReport, first["current_report_id"])
    record_report(store, date(2026, 10, 23), verdict="EVIDENCE_INCOMPLETE", evidence="c" * 64); store.commit()
    material = get_inspection_history_material(store, 1, source_event_id=original.audit_event_id,
        kind="report", ref=original.id, version="1")
    assert material.status == "available" and material.material_kind == "immutable_report"
    assert material.data["report_version"] == 1 and material.data["verdict"] == "PASSED"
    assert material.data["checks"]["cash"]["passed"] is True
    assert service.get_active_coverage(store, 1)["verified_through_trade_date"] is None


def test_rejected_acceptance_has_separately_committed_audit_and_reports_audit_outage(store, monkeypatch):
    from fastapi import HTTPException
    from app.api.routes import portfolio_inspection_coverage as routes
    preview = service.preview_reconciliation_coverage(store, 1, proposal(), now=NOW)
    request = accept_request(preview).model_copy(update={"expected_evidence_fingerprint": "f" * 64})
    # Freeze the route's service call clock to the fixture snapshot.
    real_accept = service.accept_reconciliation_coverage
    monkeypatch.setattr(routes, "accept_reconciliation_coverage", lambda db, pid, payload, operator_id: real_accept(db, pid, payload, operator_id=operator_id, now=NOW))
    with pytest.raises(HTTPException) as exc:
        routes.accept_portfolio_inspection_coverage(1, request, store, "reviewer-1")
    assert exc.value.detail["failure_audit_saved"] is True
    assert store.scalar(select(func.count()).select_from(PortfolioAccountBaseline)) == 0
    event = store.scalar(select(DataGovernanceAuditEvent).order_by(DataGovernanceAuditEvent.id.desc()))
    assert json.loads(event.attributes_json)["original_action"] == "ACCOUNT_BASELINE_ACCEPTANCE_REJECTED"
    monkeypatch.setattr(routes, "write_audit_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit down")))
    with pytest.raises(HTTPException) as unavailable:
        routes.accept_portfolio_inspection_coverage(1, request, store, "reviewer-1")
    assert unavailable.value.detail["failure_audit_saved"] is False


def test_event_arriving_during_reconciliation_is_not_consumed_by_older_frozen_demand(store):
    accepted = accepted_account(store)
    store.add(PortfolioReconciliationDay(id="pending-day", cursor_id=accepted.cursor_id, generation=1,
        target_trade_date=date(2026, 10, 23), requested_evidence_version=2, processed_evidence_version=0,
        account_source_pending=1, work_state="queued", verdict="EVIDENCE_INCOMPLETE")); store.commit()
    def save(version):
        return service.record_coverage_reconciliation(store, portfolio_id=1, trade_date=date(2026, 10, 23),
            verdict="PASSED", checks={"cash": {"passed": True}}, findings=[],
            source_refs=[{"source": "cash_ledger", "row_ids": ["1", "2"]}], evidence_hash="a" * 64,
            expected_generation=1, expected_requested_evidence_version=version, now=datetime(2026, 10, 27, 9))
    first = save(1); store.commit()
    day = store.get(PortfolioReconciliationDay, "pending-day")
    assert first["pending_new_evidence"] and first["verified_through_trade_date"] is None
    assert (day.requested_evidence_version, day.processed_evidence_version, day.work_state) == (2, 1, "queued")
    assert day.account_source_pending == 1
    repeat = save(1); store.commit()
    assert repeat["idempotent"] and repeat["pending_new_evidence"]
    assert repeat["current_report_id"] == first["current_report_id"]
    assert store.scalar(select(func.count()).select_from(PortfolioReconciliationReport)) == 1
    second = save(2); store.commit()
    assert not second["idempotent"] and second["report_version"] == 2
    assert not second["pending_new_evidence"] and second["verified_through_trade_date"] == date(2026, 10, 23)
    assert (day.requested_evidence_version, day.processed_evidence_version, day.work_state) == (2, 2, "done")
    assert day.account_source_pending == 0
    assert store.get(PortfolioReconciliationReport, first["current_report_id"]).evidence_version == 1
    with pytest.raises(service.CoverageError) as stale:
        save(1)
    assert stale.value.code == "COVERAGE_EVIDENCE_VERSION_STALE"
