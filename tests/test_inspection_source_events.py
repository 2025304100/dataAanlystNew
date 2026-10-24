"""Source transactions, receipt replay and late evidence use isolated stores."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import InvalidRequestError
from app.models.async_task import AsyncTaskRecord
from app.models.portfolio import Position, Portfolio
from app.models.sim_account import CashLedger
from app.models.portfolio_inspection_workflow import ReconciliationSourceEvent
from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationCursor, PortfolioReconciliationDay
from app.services.portfolio_reconciliation_events import consume_source_events, pending_source_events, acknowledge_baseline_sources
from tests.test_portfolio_reconciliation_job import store, DAY
from tests.test_portfolio_reconciliation_coverage import store as coverage_store, proposal, accept_request, NOW

pytestmark = pytest.mark.whitebox


def receipts(db, pid):
    return list(db.scalars(select(ReconciliationSourceEvent).where(ReconciliationSourceEvent.portfolio_id == pid)))


def cursor(db, pid):
    row = PortfolioReconciliationCursor(id="event-cursor", portfolio_id=pid, scope_key="account", generation=1,
        baseline_id="retained-baseline", coverage_start_trade_date=DAY, verified_through_trade_date=DAY.replace(day=22),
        calendar_version="real-calendar", policy_version="test", row_version=1)
    db.add(row)
    db.flush()
    acknowledge_baseline_sources(db, pid, row.baseline_id, row.generation)
    return row


def test_orm_source_and_receipt_rollback_together(store):
    with store.factory() as db:
        count = len(receipts(db, store.pid))
        db.add(CashLedger(portfolio_id=store.pid, entry_type="deposit", amount=10, balance_after=100010))
        db.flush()
        assert len(receipts(db, store.pid)) == count + 1
        assert receipts(db, store.pid)[-1].source_key
        db.rollback()
        assert len(receipts(db, store.pid)) == count
        assert db.scalar(select(func.count()).select_from(CashLedger)) == 1


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "partial"])
def test_core_task_terminal_updates_are_durable_even_without_successful_run(store, status):
    with store.factory() as db:
        db.add(AsyncTaskRecord(id="execution-event", task_type="portfolio_auto_simulation", status="queued", stage="queued",
            payload_json=json.dumps({"portfolio_id": store.pid, "trade_date": DAY.isoformat()})))
        db.commit()
        before = len(receipts(db, store.pid))
        db.execute(update(AsyncTaskRecord).where(AsyncTaskRecord.id == "execution-event").values(status=status))
        db.commit()
        rows = receipts(db, store.pid)
        assert len(rows) == before + 1
        facts = json.loads(rows[-1].facts_json)
        assert facts["before"]["status"] == "queued" and facts["after"]["status"] == status
        assert rows[-1].source_key == "execution-event"


def test_core_source_delete_keeps_original_business_day_and_owner(store):
    with store.factory() as db:
        db.execute(delete(CashLedger).where(CashLedger.portfolio_id == store.pid))
        db.commit()
        deleted = [r for r in receipts(db, store.pid) if r.operation == "delete"]
        assert len(deleted) == 1
        assert json.loads(deleted[0].facts_json)["before"]["entry_type"] == "deposit"


def test_late_source_reopens_all_later_dates_and_replay_is_idempotent(store):
    with store.factory() as db:
        scope = cursor(db, store.pid)
        for n in (20, 21, 22):
            db.add(PortfolioReconciliationDay(id=f"day-{n}", cursor_id=scope.id, generation=1, target_trade_date=DAY.replace(day=n),
                work_state="done", verdict="PASSED", requested_evidence_version=1, processed_evidence_version=1))
        db.commit()
        p = db.get(Portfolio, store.pid)
        p.last_reconciled_trade_date = DAY.replace(day=22)
        db.add(CashLedger(portfolio_id=store.pid, entry_type="deposit", amount=10, balance_after=100010,
            created_at=datetime(2026, 10, 20, 2)))
        db.commit()
        assert pending_source_events(db, store.pid)["count"] == 1
        result = consume_source_events(db, portfolio_id=store.pid)
        db.commit()
        assert result["processed"] == 1
        assert scope.verified_through_trade_date is None
        assert p.last_reconciled_trade_date == DAY.replace(day=22)  # Original legacy fact stays intact.
        days = list(db.scalars(select(PortfolioReconciliationDay).where(PortfolioReconciliationDay.cursor_id == scope.id)))
        assert len(days) == 3 and all(d.requested_evidence_version == 2 and d.processed_evidence_version == 1 for d in days)
        assert all(d.work_state == "queued" for d in days)
        assert all(d.account_source_pending == 1 for d in days)
        assert consume_source_events(db, portfolio_id=store.pid)["processed"] == 0
        assert all(d.requested_evidence_version == 2 for d in days)


def test_receipt_failure_rolls_back_core_source_even_if_caller_catches_and_commits(store):
    with store.factory() as db:
        cursor(db, store.pid)
        db.commit()
    def fail_receipt(conn, sql_cursor, statement, params, context, many):
        if statement.lstrip().startswith("INSERT INTO reconciliation_source_events"):
            raise RuntimeError("isolated receipt store unavailable")
    event.listen(store.engine, "before_cursor_execute", fail_receipt)
    try:
        with store.factory() as db:
            with pytest.raises(RuntimeError, match="receipt store"):
                db.execute(CashLedger.__table__.insert().values(portfolio_id=store.pid, entry_type="deposit", amount=10, balance_after=100010))
            with pytest.raises(InvalidRequestError, match="inactive"):
                db.commit()
            db.rollback()
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_receipt)
    with store.factory() as db:
        assert db.scalar(select(func.count()).select_from(CashLedger)) == 1


def test_consumer_crash_rolls_back_demand_and_receipt_processing(store, monkeypatch):
    import app.services.data_governance_audit as audit
    with store.factory() as db:
        scope = cursor(db, store.pid)
        db.commit()
        db.add(CashLedger(portfolio_id=store.pid, entry_type="deposit", amount=10, balance_after=100010, created_at=datetime(2026, 10, 20)))
        db.commit()
        original = audit.write_audit_event
        monkeypatch.setattr(audit, "write_audit_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("consumer interrupted")))
        with pytest.raises(RuntimeError, match="interrupted"):
            consume_source_events(db, portfolio_id=store.pid)
        db.rollback()
        assert pending_source_events(db, store.pid)["count"] == 1
        assert db.get(PortfolioReconciliationCursor, scope.id).verified_through_trade_date == DAY.replace(day=22)
        monkeypatch.setattr(audit, "write_audit_event", original)
        assert consume_source_events(db, portfolio_id=store.pid)["processed"] == 1


def test_missing_calendar_retains_receipt_without_inventing_day(store, monkeypatch):
    import app.services.portfolio_inspection_diagnostics as diagnostics
    with store.factory() as db:
        scope = cursor(db, store.pid)
        db.commit()
        db.add(CashLedger(portfolio_id=store.pid, entry_type="deposit", amount=10, balance_after=100010))
        db.commit()
        monkeypatch.setattr(diagnostics, "resolve_inspection_calendar", lambda *a, **k: {"status": "CALENDAR_UNKNOWN"})
        assert consume_source_events(db, portfolio_id=store.pid)["processed"] == 0
        db.commit()
        assert receipts(db, store.pid)[-1].status == "waiting_calendar"
        assert scope.verified_through_trade_date == DAY.replace(day=22)
        assert not list(db.scalars(select(PortfolioReconciliationDay)))


def test_real_baseline_acceptance_acknowledges_existing_receipts_without_passing_history(coverage_store):
    from app.services.portfolio_reconciliation_coverage import accept_reconciliation_coverage, preview_reconciliation_coverage
    db = coverage_store
    assert pending_source_events(db, 1)["count"] > 0
    preview = preview_reconciliation_coverage(db, 1, proposal(), now=NOW)
    accepted = accept_reconciliation_coverage(db, 1, accept_request(preview), operator_id="reviewer", now=NOW)
    db.commit()
    assert pending_source_events(db, 1)["count"] == 0
    assert all(row.status == "covered_by_baseline" for row in receipts(db, 1))
    assert db.get(PortfolioReconciliationCursor, accepted.cursor_id).verified_through_trade_date is None


def test_job_read_never_autoflushes_source_or_marks_receipt_processed(store):
    from app.services.portfolio_reconciliation_job import get_portfolio_reconciliation_jobs
    with store.factory() as db:
        db.get(Portfolio, store.pid).name = "not-saved"
        flushed = []
        event.listen(db, "before_flush", lambda *a: flushed.append(True))
        snapshot = get_portfolio_reconciliation_jobs(db, store.pid)
        assert not flushed and snapshot["source_events"]["count"] == 1
        assert receipts(db, store.pid)[0].status == "pending"


def test_event_consumption_does_not_clear_the_account_buy_guard_before_real_check(store,monkeypatch):
    import app.services.portfolio_reconciliation_activation as activation
    from app.services.portfolio_reconciliation_events import assert_account_sources_checked_for_buy
    from app.services.portfolio_reconciliation_coverage import CoverageError
    with store.factory() as db:
        scope=cursor(db,store.pid);db.commit()
        active={"cursor_id":scope.id,"generation":1}
        monkeypatch.setattr(activation,"_active",lambda *a,**k:active)
        db.add(CashLedger(portfolio_id=store.pid,entry_type="deposit",amount=10,balance_after=100010));db.commit()
        with pytest.raises(CoverageError,match="等待自动核对"):
            assert_account_sources_checked_for_buy(db,store.pid)
        consume_source_events(db,portfolio_id=store.pid);db.commit()
        assert pending_source_events(db,store.pid)["count"]==0
        with pytest.raises(CoverageError,match="等待自动核对"):
            assert_account_sources_checked_for_buy(db,store.pid)


def test_schema_migration_adds_receipts_associations_and_pending_guard_to_old_days(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path=Path("alembic/versions/2026_10_23_0076_inspection_source_evidence.py")
    spec=importlib.util.spec_from_file_location("inspection_0076",path);migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=create_engine("sqlite:///"+str(tmp_path/"isolated-0076.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE portfolio_reconciliation_days (id VARCHAR(64) PRIMARY KEY)"))
        migration.op=Operations(MigrationContext.configure(conn))
        migration.upgrade();migration.upgrade()
        assert {"reconciliation_source_events","inspection_evidence_requests","inspection_evidence_decisions"} <= set(inspect(conn).get_table_names())
        assert "account_source_pending" in {c["name"] for c in inspect(conn).get_columns("portfolio_reconciliation_days")}
        with pytest.raises(RuntimeError,match="Keep source receipts"):
            migration.downgrade()
    engine.dispose()
