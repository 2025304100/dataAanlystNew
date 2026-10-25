"""Installed-coverage order lane on a fresh, fully migrated MySQL 5.7 schema.

Only clocks are controlled. Coverage acceptance, activation, calendar checks,
source receipts, composite admission, fees, market rules and order writes are
real. The manual-account scenario has no automatic strategy admission scope;
its data/binding checks must explicitly prove that scope is not applicable.
The annual calendar is a synthetic adapter fixture, never production data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting
from app.models.daily_bar import DailyBar
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_reconciliation_coverage import PortfolioAccountBaseline, PortfolioReconciliationDay
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.schemas.portfolio_reconciliation_coverage import CoverageAcceptRequest, CoverageHistoryGap, CoveragePreviewRequest
from app.services import portfolio_reconciliation_activation as activation
from app.services import portfolio_reconciliation_coverage as coverage
from app.services.portfolio_reconciliation_events import install_source_receipts, pending_source_events
from app.services.portfolio_state_machine import _set_status
from app.services.sim_accounts import cash_balance, place_sim_order

pytestmark = pytest.mark.whitebox
ROOT = Path(__file__).resolve().parents[1]
TARGET = "wps_0024_083_combination_contracts"
BASELINE_AT = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
START = date(2026, 10, 23)
EXECUTION_AT = datetime(2026, 10, 23, 2, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def e2e_engine():
    raw = os.environ.get("MYSQL57_TEST_URL")
    if not raw:
        pytest.skip("MYSQL57_TEST_URL is required for the installed-coverage lane")
    url = make_url(raw)
    if url.get_backend_name() != "mysql" or not re.fullmatch(r"codex_combination_[A-Za-z0-9_]+", url.database or ""):
        pytest.fail("Refusing a non-isolated MySQL database")
    engine = create_engine(url, hide_parameters=True)
    with engine.connect() as conn:
        assert not inspect(conn).get_table_names(), "Supply a fresh empty orders E2E database"
        assert conn.scalar(text("SELECT VERSION()")) == "5.7.26"
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", APP_DATABASE_URL=raw, ALEMBIC_DATABASE_URL=raw)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/guarded_database_migration.py"),
        "upgrade", "--expected-database", url.database, "--expected-revision", "base",
        "--expected-server-version", "5.7.26", "--target-revision", TARGET],
        env=environment, capture_output=True, encoding="utf-8", cwd=ROOT, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["after"]["revisions"] == [TARGET]
    assert all(value == "InnoDB" for value in report["after"]["table_engines"].values())
    required = {"portfolio_account_baselines", "portfolio_reconciliation_cursors",
                "data_governance_audit_events", "reconciliation_source_events", "portfolio_reconciliation_days"}
    assert required <= set(report["after"]["table_engines"])
    print("installed_coverage_database", url.database, TARGET, report["after"]["server_uuid"])
    # Explicit full annual adapter output with closures. It is synthetic test
    # evidence; no network provider or production calendar is changed.
    with Session(engine) as db:
        start = date(2026, 1, 1)
        days = [start + timedelta(days=i) for i in range(365)]
        closed = set([day for day in days if day.weekday() < 5][:21])
        opened = {day for day in days if day.weekday() < 5 and day not in closed}
        db.add_all(MarketCalendar(trade_date=day, is_trading_day=int(day in opened)) for day in days)
        db.add(AppSetting(key="market.cn_trade_calendar", updated_by="exchange-calendar-sync", value_json=json.dumps({
            "source": "https://www.sse.com.cn/disclosure/dealinstruc/closed/", "year": 2026,
            "refreshed_at": "2026-10-21T00:00:00+00:00", "trading_days": len(opened),
            "source_version": "synthetic-e2e-calendar-2026"})))
        db.commit()
    install_source_receipts()
    yield engine
    engine.dispose()


@pytest.fixture()
def clock(monkeypatch):
    from app.models import portfolio, sim_account
    from app.services import data_governance_audit, portfolio_inspection_diagnostics, portfolio_reconciliation_events, sim_accounts
    current = {"at": BASELINE_AT}

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current["at"].astimezone(tz) if tz is not None else current["at"].replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return current["at"].replace(tzinfo=None)

    # Time seams only. Do not replace admission, acceptance, inspection,
    # binding, coverage, receipt or financial functions with canned outcomes.
    for module in (portfolio, sim_account, data_governance_audit,
                   portfolio_inspection_diagnostics, portfolio_reconciliation_events, sim_accounts):
        monkeypatch.setattr(module, "datetime", ClockDateTime)
    monkeypatch.setattr(activation, "_now", lambda: current["at"].replace(tzinfo=None))
    return current


def seed_account(engine):
    with Session(engine) as db:
        token = uuid.uuid4().hex[:10]
        p = Portfolio(name=f"coverage-e2e-{token}", account_type="simulated", total_capital=100000,
                      investable_ratio=.95, cash_reserve_ratio=.05, status_reconciliation="RECONCILIATION_BLOCKED")
        s = Symbol(symbol=f"E{token}", name="Manual coverage E2E", asset_type="stock", market="CN")
        db.add_all([p, s]); db.flush()
        _set_status(db, p, "RECONCILIATION_BLOCKED")
        db.add(CashLedger(portfolio_id=p.id, entry_type="deposit", amount=100000, balance_after=100000,
                          ref_type="portfolio", ref_id=p.id, created_at=BASELINE_AT.replace(tzinfo=None)-timedelta(days=1)))
        for day in (date(2026, 10, 21), date(2026, 10, 22)):
            db.add(DailyBar(symbol_id=s.id, trade_date=day, open=10, high=10.2, low=9.8, close=10, volume=100000,
                           created_at=BASELINE_AT.replace(tzinfo=None)))
        db.commit()
        return p.id, s.id


def account_facts(db, pid):
    complete_rows = tuple((table, tuple(tuple(row.items()) for row in db.execute(
        text(f"SELECT * FROM {table} WHERE portfolio_id=:pid ORDER BY id"), {"pid": pid}).mappings()))
        for table in ("positions", "sim_orders", "sim_trades", "cash_ledger"))
    return (cash_balance(db, pid), tuple(db.execute(select(Position.symbol_id, Position.quantity, Position.avg_cost)
        .where(Position.portfolio_id == pid)).all()),
        *[db.scalar(select(func.count()).select_from(model).where(model.portfolio_id == pid))
          for model in (SimOrder, SimTrade, CashLedger)], complete_rows)


def accept_real_baseline(db, pid):
    proposal = CoveragePreviewRequest(coverage_start_trade_date=START, history_gaps=[CoverageHistoryGap(
        trade_date_from=date(2026, 10, 1), trade_date_to=date(2026, 10, 20),
        reason="隔离测试历史证据明确未知并保留审计边界", attempted_sources=["synthetic archived fixture"],
        evidence_refs=["test-only:history-unknown"])])
    preview = coverage.preview_reconciliation_coverage(db, pid, proposal, now=BASELINE_AT)
    assert preview.can_accept, [item.model_dump() for item in preview.blockers]
    accepted = coverage.accept_reconciliation_coverage(db, pid, CoverageAcceptRequest(**proposal.model_dump(),
        expected_evidence_fingerprint=preview.evidence_fingerprint, expected_cursor_row_version=preview.cursor_row_version,
        acceptance_key=f"e2e-accept-{pid}", review_note="隔离测试审核当前账户真实账本与未决义务", acknowledge_history_unknown=True),
        operator_id="isolated-test-reviewer", now=BASELINE_AT)
    db.commit()
    assert coverage.get_active_coverage(db, pid)["baseline_id"] == accepted.baseline_id
    assert pending_source_events(db, pid)["material_count"] == 0
    return accepted


def activate_real_coverage(db, pid):
    review = activation.get_coverage_activation_review(db, pid)
    assert review["can_confirm"], review
    checks = {item["key"]: item for item in review["checks"]}
    assert checks["account_sources"]["passed"] and checks["calendar"]["passed"]
    assert checks["data"]["passed"] is True and checks["data"]["has_admission_scope"] is False
    assert checks["binding"]["passed"] is True and checks["binding"]["not_applicable"] is True
    result = activation.confirm_initial_coverage_review(db, pid, baseline_id=review["baseline_id"], generation=review["generation"],
        expected_cursor_row_version=review["cursor_row_version"], expected_evidence_fingerprint=review["evidence_fingerprint"],
        activation_key=f"e2e-activate-{pid}", acknowledge_new_scope=True,
        review_note="隔离测试本人核验账户及日历并启用新范围", operator_id="isolated-test-reviewer")
    db.commit()
    assert result["scope_authorized"] and result["reconciliation_passed"] is False
    assert activation.get_initial_coverage_activation(db, pid) is not None
    return result


def assert_buy_blocked(engine, pid, sid, code):
    with Session(engine) as db:
        before = account_facts(db, pid)
        with pytest.raises(HTTPException) as error:
            place_sim_order(db, db.get(Portfolio, pid), db.get(Symbol, sid), "buy", 100, price=10)
        assert error.value.status_code == 409
        assert error.value.detail["error_code"] == code
        assert account_facts(db, pid) == before
        db.rollback()
    with Session(engine) as db:
        assert account_facts(db, pid) == before


def test_installed_coverage_without_activation_or_before_start_blocks_zero_trades(e2e_engine, clock):
    pid, sid = seed_account(e2e_engine)
    with Session(e2e_engine) as db:
        accept_real_baseline(db, pid)
    clock["at"] = EXECUTION_AT
    assert_buy_blocked(e2e_engine, pid, sid, "INITIAL_COVERAGE_REVIEW_REQUIRED")
    clock["at"] = BASELINE_AT
    with Session(e2e_engine) as db:
        activate_real_coverage(db, pid)
    assert_buy_blocked(e2e_engine, pid, sid, "OUTSIDE_CURRENT_COVERAGE")


def test_accepted_activated_scope_buys_with_fees_and_preserves_history(e2e_engine, clock):
    pid, sid = seed_account(e2e_engine)
    with Session(e2e_engine) as db:
        accepted = accept_real_baseline(db, pid)
        activate_real_coverage(db, pid)
    clock["at"] = EXECUTION_AT
    with Session(e2e_engine) as db:
        order, trade = place_sim_order(db, db.get(Portfolio, pid), db.get(Symbol, sid), "buy", 100, price=10)
        db.commit()
        assert order.review_status is None  # Manual order must not invent APPROVED.
        assert order.fee > 0 and order.filled_quantity == trade.quantity == 100
        assert order.filled_at.date() == START and order.created_at.date() == START
        assert trade.created_at.date() == START
        assert cash_balance(db, pid) == pytest.approx(100000-order.filled_amount-order.fee, abs=.005, rel=0)
        assert db.scalar(select(Position.quantity).where(Position.portfolio_id == pid)) == 100
        assert account_facts(db, pid)[2:5] == (1, 1, 2)
        assert pending_source_events(db, pid)["material_count"] > 0
        active = coverage.get_active_coverage(db, pid)
        assert active["baseline_id"] == accepted.baseline_id and active["generation"] == 1
        assert active["history_gaps"] and active["verified_through_trade_date"] is None
        assert db.scalar(select(func.count()).select_from(PortfolioReconciliationDay)
                         .where(PortfolioReconciliationDay.cursor_id == accepted.cursor_id)) == 0
    # A later independent buy must observe real committed source receipts.
    assert_buy_blocked(e2e_engine, pid, sid, "RECONCILIATION_SOURCE_PENDING")


def test_installed_coverage_keeps_composite_blocker_effective(e2e_engine, clock):
    pid, sid = seed_account(e2e_engine)
    with Session(e2e_engine) as db:
        accept_real_baseline(db, pid)
        activate_real_coverage(db, pid)
        db.get(Portfolio, pid).status_data = "DATA_INCOMPLETE_PAUSED"
        db.commit()
    clock["at"] = EXECUTION_AT
    assert_buy_blocked(e2e_engine, pid, sid, "COMPOSITE_BLOCK_LEVEL_TOO_HIGH")


def test_invalid_baseline_fails_closed_without_deleting_evidence(e2e_engine, clock):
    pid, sid = seed_account(e2e_engine)
    with Session(e2e_engine) as db:
        accepted = accept_real_baseline(db, pid)
        baseline = db.get(PortfolioAccountBaseline, accepted.baseline_id)
        original_hash, original_snapshot = baseline.content_hash, baseline.snapshot_json
        # FK enforcement already forbids an absent referenced baseline. Keep
        # its row and original snapshot, then prove a hash mismatch is blocked.
        # ORM baselines are immutable. Deliberately simulate external storage
        # corruption via SQL, only on this test's row; never use it to pass a gate.
        db.execute(text("UPDATE portfolio_account_baselines SET content_hash=:bad WHERE id=:id"),
                   {"bad": "0" * 64, "id": accepted.baseline_id})
        db.commit()
    clock["at"] = EXECUTION_AT
    try:
        assert_buy_blocked(e2e_engine, pid, sid, "BASELINE_SOURCE_INVALID")
    finally:
        with Session(e2e_engine) as db:
            db.execute(text("UPDATE portfolio_account_baselines SET content_hash=:original WHERE id=:id"),
                       {"original": original_hash, "id": accepted.baseline_id})
            db.commit()
            assert db.get(PortfolioAccountBaseline, accepted.baseline_id).snapshot_json == original_snapshot
