"""A mature pool may run even when unrelated symbols have unusable history."""
import json
from datetime import date, timedelta

import pytest

from test_m1_closure import mining_env, warehouse_path
from test_factor_mining_routes import _client
from test_mining_runtime_integrity import _inputs, _seed, _wait
from app.services.factors.store import FactorWarehouse, SHARED_TARGET_BATCH_ID

pytestmark = pytest.mark.whitebox


def test_readonly_pool_preflight_and_real_worker_use_the_same_available_signal_dates(mining_env, warehouse_path, monkeypatch):
    db = mining_env
    _seed(db)
    wh = FactorWarehouse(warehouse_path)
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", warehouse_path)
    # Four early days contain only an unrelated, then suspended symbol. They
    # have no tradable target at all. The pool still has 393+ mature points.
    with wh.connection() as conn:
        conn.execute("INSERT INTO raw_daily_bars(symbol,trade_date,adjust,open,high,low,close,volume,amount,"
            "source,source_origin,source_row_id,ingested_at,batch_id) "
            "SELECT 'OUTSIDE', DATE '2026-01-01' + CAST(i AS INTEGER), 'qfq',10,11,9,10,100,1000,"
            "'test','daily_bars',90000+i,TIMESTAMP '2026-01-01','test' FROM range(0,4) t(i)")
        conn.execute("DELETE FROM factor_targets")
        conn.execute("DELETE FROM warehouse_label_coverage")
    client = _client(db)
    payload = _inputs(warehouse_path, finalize=0)
    payload["start_date"] = "2026-01-01T00:00:00"
    budget = client.post("/factor-mining/split-budget", json={
        "candidate_pool_snapshot_id": payload["candidate_pool_snapshot_id"],
        "start_date": payload["start_date"], "end_date": payload["end_date"],
        "data_cutoff_at": payload["data_cutoff_at"], "target_horizon": 5,
        "frequency": "daily",
    })
    assert budget.status_code == 200 and budget.json()["meets_floor"] is True, budget.text
    with wh.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM factor_targets").fetchone()[0] == 0
    created = client.post("/factor-mining/runs", json=payload)
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    assert created.json()["split_budget"]["total_points"] == 393
    assert created.json()["split_budget"]["total_points"] == budget.json()["total_points"]
    done = _wait(client, db, run_id, lambda run: run["status"] in ("succeeded", "failed"))
    assert done["status"] == "succeeded", done
    with wh.connection(read_only=True) as conn:
        batches = conn.execute("SELECT DISTINCT calc_batch_id FROM factor_targets").fetchall()
        assert batches == [(SHARED_TARGET_BATCH_ID,)]
        assert conn.execute("SELECT COUNT(*) FROM factor_targets WHERE symbol='OUTSIDE'").fetchone()[0] == 0
