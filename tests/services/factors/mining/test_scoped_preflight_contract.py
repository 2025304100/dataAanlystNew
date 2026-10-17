"""Public preflight HTTP contracts; all business data is isolated in temporary stores."""
from datetime import date, datetime, timedelta
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import factor_mining as FM
from app.db.session import get_db
from app.models.mining_candidate_pool import TrainingCandidatePool, TrainingCandidatePoolSnapshot
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


@pytest.fixture
def scoped_preflight(db_session, tmp_path, monkeypatch):
    path = tmp_path / "preflight.duckdb"
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(path))
    wh = FactorWarehouse(path)
    start = date(2025, 1, 1)
    bars = []
    for offset in range(12):
        for symbol in (["000001", "999999"] if offset < 6 else ["999999"]):
            bars.append(dict(symbol=symbol, trade_date=start + timedelta(days=offset),
                adjust="qfq", open=10, high=11, low=9, close=10, volume=100,
                amount=1000, turnover_rate=1, source="test", source_origin="daily_bars",
                source_row_id=len(bars) + 1, ingested_at=datetime(2025, 1, 1), batch_id="test"))
    wh.upsert_daily_bars(bars, source_key="test", watermark=len(bars))
    from app.services.factors.config import ensure_factor_system_config
    ensure_factor_system_config(db_session).warehouse_path = str(path)
    db_session.add(TrainingCandidatePool(id="pool-preflight", name="isolated", source_type="filter",
        status="frozen", member_count=1))
    db_session.add(TrainingCandidatePoolSnapshot(id="snap-preflight", pool_id="pool-preflight",
        members_json=json.dumps([{"symbol": "000001"}]), rule_hash="test", is_locked=1,
        member_count=1, data_cutoff_at=datetime(2025, 1, 12)))
    db_session.commit()
    app = FastAPI()
    app.include_router(FM.router)
    from app.api.routes import factor_mining_wizard as wizard_routes
    app.include_router(wizard_routes.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app), wh


def test_budget_counts_only_tradable_mature_labels_in_frozen_pool_without_writing(scoped_preflight):
    client, wh = scoped_preflight
    response = client.post("/factor-mining/split-budget", json={
        "candidate_pool_snapshot_id": "snap-preflight", "start_date": "2025-01-01T00:00:00",
        "end_date": "2025-01-12T00:00:00", "data_cutoff_at": "2025-01-12T00:00:00",
        "frequency": "daily", "target_horizon": 1,
    })
    assert response.status_code == 200, response.text
    assert response.json()["total_points"] == 5  # Pool bars stop Jan 6; only Jan 1..5 can exit.
    assert response.json()["candidate_symbol_count"] == 1
    with wh.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM factor_targets").fetchone()[0] == 0


def test_field_validation_cannot_use_other_symbols_or_future_snapshots(scoped_preflight):
    import time
    client, wh = scoped_preflight
    with wh.connection() as conn:
        conn.execute("INSERT INTO raw_valuation_snapshots(symbol,trade_date,pe_ttm,source,source_hash,ingested_at,batch_id) VALUES "
            "('999999', '2025-01-01', 10,'test','a','2025-01-01','test'), "
            "('000001', '2025-01-15', 20,'test','b','2025-01-01','test')")
    response = client.post("/factor-mining/validations", json={"draft_id": "scope-check",
        "config": {"candidate_pool_snapshot_id": "snap-preflight", "start_date": "2025-01-01",
            "end_date": "2025-01-12", "data_cutoff_at": "2025-01-12", "target_horizon": 1,
            "rebalance_frequency": "daily"}, "fields": ["close", "pe_ttm"]})
    assert response.status_code == 201, response.text
    task_id = response.json()["task_id"]
    for _ in range(100):
        progress = client.get(f"/factor-mining/validations/{task_id}").json()
        if progress.get("status") in ("done", "failed", "cancelled"):
            break
        time.sleep(.05)
    assert progress["status"] == "done", progress
    assert progress["report"]["verdict"] == "block"
    fields = {s["field"]: s["result"] for s in progress["report"]["shards"]}
    assert fields["close"].get("coverage") == 1, json.dumps(progress, ensure_ascii=False)
    assert fields["pe_ttm"]["coverage"] == 0


def test_submission_rechecks_pool_budget_before_creating_a_run(scoped_preflight, monkeypatch):
    client, wh = scoped_preflight
    calls = []
    from types import SimpleNamespace
    monkeypatch.setattr(FM.mining_runner, "submit_mining_run", lambda *a, **kw: (
        calls.append(kw) or SimpleNamespace(task_id="never", queue_position=0)))
    before = client.get("/factor-mining/runs").json()["total"]
    response = client.post("/factor-mining/runs", json={
        "candidate_pool_snapshot_id": "snap-preflight", "start_date": "2025-01-01T00:00:00",
        "end_date": "2025-01-12T00:00:00", "data_cutoff_at": "2025-01-12T00:00:00",
        "rebalance_frequency": "daily", "target_horizon": 1,
        "filter_config": {"selected_fields": ["close"], "warehouse_path": str(wh.path)},
    })
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error_code"] == "MINING_PREFLIGHT_BUDGET_BLOCKED"
    assert calls == []
    assert client.get("/factor-mining/runs").json()["total"] == before


def test_submission_rechecks_field_coverage_even_with_sufficient_budget(scoped_preflight, db_session, monkeypatch):
    client, wh = scoped_preflight
    with wh.connection() as conn:
        conn.execute("INSERT INTO raw_daily_bars(symbol,trade_date,adjust,open,high,low,close,volume,amount,"
            "source,source_origin,source_row_id,ingested_at,batch_id) "
            "SELECT '000001', DATE '2025-01-01' + CAST(i AS INTEGER), 'qfq',10,11,9,10,100,1000,"
            "'test','daily_bars',100+i,TIMESTAMP '2025-01-01','test' FROM range(6,366) t(i)")
    db_session.get(TrainingCandidatePoolSnapshot, "snap-preflight").data_cutoff_at = datetime(2026, 1, 1)
    db_session.commit()
    calls = []
    from types import SimpleNamespace
    monkeypatch.setattr(FM.mining_runner, "submit_mining_run", lambda *a, **kw: (
        calls.append(kw) or SimpleNamespace(task_id="never", queue_position=0)))
    response = client.post("/factor-mining/runs", json={
        "candidate_pool_snapshot_id": "snap-preflight", "start_date": "2025-01-01T00:00:00",
        "end_date": "2025-12-01T00:00:00", "data_cutoff_at": "2026-01-01T00:00:00",
        "rebalance_frequency": "daily", "target_horizon": 1,
        "filter_config": {"selected_fields": ["close", "pe_ttm"], "warehouse_path": str(wh.path)},
    })
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error_code"] == "MINING_PREFLIGHT_FIELDS_BLOCKED"
    assert calls == []
    assert client.get("/factor-mining/runs").json()["total"] == 0


@pytest.mark.parametrize("horizon,cutoff,expected", [(1,"2025-01-04",3), (5,"2025-01-12",1), (20,"2025-01-12",0)])
def test_pool_budget_obeys_real_horizon_and_frozen_exit_cutoff(scoped_preflight, horizon, cutoff, expected):
    client, _ = scoped_preflight
    response = client.post("/factor-mining/split-budget", json={
        "candidate_pool_snapshot_id": "snap-preflight", "start_date": "2025-01-01T00:00:00",
        "end_date": cutoff + "T00:00:00", "data_cutoff_at": cutoff + "T00:00:00",
        "frequency": "daily", "target_horizon": horizon,
    })
    assert response.status_code == 200, response.text
    assert response.json()["total_points"] == expected


def test_scoped_budget_rejects_equal_dates_as_input_error(scoped_preflight):
    client, _ = scoped_preflight
    response = client.post("/factor-mining/split-budget", json={
        "candidate_pool_snapshot_id": "snap-preflight", "start_date": "2025-01-01T00:00:00",
        "end_date": "2025-01-01T00:00:00", "data_cutoff_at": "2025-01-12T00:00:00",
        "frequency": "daily", "target_horizon": 1,
    })
    assert response.status_code == 422, response.text
