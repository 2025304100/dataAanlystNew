"""The public latest-candidates route must consume actual completed fast scans."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.api.routes.discovery import get_latest_candidates
from app.models.discovery import DiscoveryTaskRecord
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.discovery_score_snapshot import DiscoveryScoreSnapshot, DiscoveryScoreSnapshotItem
from app.models.scan import ScanResult, ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.services.discovery_fast_scan import run_fast_scan


pytestmark = pytest.mark.whitebox


def _ready_snapshot(db, *, scope="cn-etf", codes=("510300", "510500")):
    region, asset = scope.replace("_", "-").split("-")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    snapshot = DiscoveryScoreSnapshot(scope=scope.replace("-", "_"), status="ready",
        trade_date=now, generated_at=now, symbol_count=len(codes), coverage_pct=100)
    db.add(snapshot)
    db.flush()
    for code in codes:
        symbol = Symbol(symbol=code, name=f"Candidate {code}", asset_type=asset,
                        market="sh" if region == "cn" else "nasdaq")
        universe = UniverseSymbol(symbol=code, name=code, asset_type=asset,
                                  region=region, market=symbol.market)
        db.add_all([symbol, universe])
        db.flush()
        db.add(DiscoveryScoreSnapshotItem(snapshot_id=snapshot.id, symbol_id=symbol.id,
            universe_symbol_id=universe.id, quality_score=80, timing_score=75,
            priority_score=77, stage="start", action="open"))
    db.commit()
    return snapshot


def _legacy_scan(db, *, code="159915", stored_candidate=True, finished_at=None):
    now = finished_at or datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    symbol = Symbol(symbol=code, name=code, asset_type="etf", market="sz")
    universe = UniverseSymbol(symbol=code, name=code, asset_type="etf", region="cn", market="sz")
    run = ScanRun(run_name="legacy-discovery", scope_snapshot="cn-etf", status="done", finished_at=now)
    db.add_all([symbol, universe, run])
    db.flush()
    db.add(DiscoveryTaskRecord(id=f"legacy-{run.id}", scope="cn-etf", status="done",
        stage="done", scan_run_id=run.id, finished_at=now))
    db.add(ScanResult(scan_run_id=run.id, symbol_id=symbol.id, result_type="quality", rank_no=1,
        quality_score=90, timing_score=90, priority_score=91, stage="hold", action="hold"))
    if stored_candidate:
        db.add(DiscoveryCandidate(scan_run_id=run.id, universe_symbol_id=universe.id, symbol=code,
            name=code, asset_type="etf", quality_score=92, timing_score=92, priority_score=93,
            stage="hold", action="hold", is_promoted=1))
    db.commit()
    return run


def test_latest_candidates_reads_real_fast_scan_without_a_legacy_task_or_writes(db_session):
    _ready_snapshot(db_session)
    scan = run_fast_scan(scope="cn-etf", min_score=0, db=db_session)
    assert scan["status"] == "ok" and scan["result_rows_written"] == 2
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().upper())

    event.listen(db_session.get_bind(), "before_cursor_execute", capture)
    try:
        rows = get_latest_candidates(min_score=0, limit=50, scope="cn-etf", db=db_session)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", capture)
    assert {row["symbol"] for row in rows} == {"510300", "510500"}
    assert all(row["priority_score"] == 77 for row in rows)
    assert all(row["candidate_id"] is None for row in rows)
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements)


def test_new_fast_scan_takes_precedence_over_old_legacy_candidates(db_session):
    _legacy_scan(db_session)
    _ready_snapshot(db_session)
    scan = run_fast_scan(scope="cn-etf", min_score=0, db=db_session)
    assert scan["result_rows_written"] == 2
    rows = get_latest_candidates(min_score=0, limit=50, scope="cn-etf", db=db_session)
    assert {row["symbol"] for row in rows} == {"510300", "510500"}


def test_latest_fast_scan_is_isolated_by_snapshot_scope_including_superseded_versions(db_session):
    cn = _ready_snapshot(db_session, codes=("510300",))
    run_fast_scan(scope="cn-etf", min_score=0, db=db_session)
    _ready_snapshot(db_session, scope="us-stock", codes=("AAPL",))
    run_fast_scan(scope="us-stock", min_score=0, db=db_session)
    cn.status = "superseded"
    db_session.commit()
    for scope in ("cn-etf", "cn_etf"):
        assert [r["symbol"] for r in get_latest_candidates(min_score=0, limit=50, scope=scope, db=db_session)] == ["510300"]
    assert [r["symbol"] for r in get_latest_candidates(min_score=0, limit=50, scope="us-stock", db=db_session)] == ["AAPL"]
    assert get_latest_candidates(min_score=0, limit=50, scope="cn-stock", db=db_session) == []


def test_new_empty_fast_scan_does_not_resurrect_older_fast_or_legacy_candidates(db_session):
    _legacy_scan(db_session)
    _ready_snapshot(db_session)
    first = run_fast_scan(scope="cn-etf", min_score=0, db=db_session)
    assert first["result_rows_written"] == 2
    empty = run_fast_scan(scope="cn-etf", min_score=99, db=db_session)
    assert empty["status"] == "ok" and empty["result_rows_written"] == 0
    assert get_latest_candidates(min_score=0, limit=50, scope="cn-etf", db=db_session) == []


def test_completed_legacy_task_without_a_run_keeps_its_existing_empty_result_contract(db_session):
    _legacy_scan(db_session)
    db_session.add(DiscoveryTaskRecord(id="legacy-completed-without-results", scope="cn-etf",
        status="done", stage="done", finished_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db_session.commit()
    assert get_latest_candidates(min_score=0, limit=50, scope="cn-etf", db=db_session) == []


@pytest.mark.parametrize("stored_candidate, expected_score", [(True, 93), (False, 91)])
def test_newer_legacy_scan_keeps_stored_candidate_and_quality_fallback_contracts(db_session, stored_candidate, expected_score):
    _ready_snapshot(db_session)
    run_fast_scan(scope="cn-etf", min_score=0, db=db_session)
    _legacy_scan(db_session, stored_candidate=stored_candidate,
                 finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
    rows = get_latest_candidates(min_score=0, limit=50, scope="cn-etf", db=db_session)
    assert [row["symbol"] for row in rows] == ["159915"]
    assert rows[0]["priority_score"] == expected_score
    assert (rows[0]["candidate_id"] is not None) == stored_candidate


@pytest.mark.parametrize("unusable_status", ["building", "failed"])
def test_scan_with_unpublished_snapshot_is_not_a_latest_candidate_source(db_session, unusable_status):
    snapshot = _ready_snapshot(db_session)
    run_fast_scan(scope="cn-etf", min_score=0, db=db_session)
    snapshot.status = unusable_status
    db_session.commit()
    assert get_latest_candidates(min_score=0, limit=50, scope="cn-etf", db=db_session) == []
