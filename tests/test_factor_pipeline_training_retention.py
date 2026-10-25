"""Research-only training preserves unrelated score and warehouse history.

The scoring task API, worker, mirrors, DSL, labels, fit and retention run for real
against temporary SQLite/DuckDB stores. Only worker dispatch is synchronous.
"""
from datetime import date, datetime, timedelta

import pytest

from tests.test_turnover_v3_public_warehouse_lifecycle import (
    turnover_client, FORMULA, _seed_turnover_market,
)

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("materialize_scores", [False, True, None],
                         ids=["scores_false", "scores_true", "scores_default"])
def test_training_and_general_refresh_preserve_all_unrelated_history(
    turnover_client, db_session, tmp_path, materialize_scores,
):
    from app.models.score import Score
    from app.services.factors.definitions import seed_factor_definitions
    from app.services.factors.store import FactorWarehouse

    seed_factor_definitions(db_session)
    db_session.commit()
    days, symbol_ids, _watchlist = _seed_turnover_market(db_session, tmp_path)
    client = turnover_client
    version = client.post("/api/v1/factors/turnover_z20/versions", json={
        "formula_expr": FORMULA, "params": {}, "direction": "nonlinear",
        "source_mapping": {"table": "raw_daily_bars", "field": "turnover_rate"},
        "postprocess": {"winsorize": {"method": "mad", "mad_multiplier": 3.0},
                        "zscore": {"ddof": 0}, "missing_policy": "exclude"},
    })
    assert version.status_code == 201, version.text
    created = client.post("/api/v1/factor-sets", json={"name": "Research must preserve history"})
    assert created.status_code == 200, created.text
    set_id = created.json()["id"]
    member = client.post(f"/api/v1/factor-sets/{set_id}/members", json={
        "factor_code": "turnover_z20", "factor_version": version.json()["version"],
        "role": "feature", "missing_policy": "exclude",
    })
    assert member.status_code == 200, member.text
    frozen = client.post(f"/api/v1/scoring/factorsets/{set_id}/freeze", json={})
    assert frozen.status_code == 200, frozen.text

    # More than 30 unreferenced score batches ensure ordinary retention has
    # real history to delete. Current-score protection retains only the newest.
    old_date = date(2024, 1, 2)
    old_time = datetime(2024, 1, 2)
    for index in range(32):
        stamp = old_time + timedelta(days=index)
        db_session.add(Score(
            symbol_id=symbol_ids[0], trade_date=old_date + timedelta(days=index),
            quality_score=70, quality_grade="B", timing_score=70, stage="start",
            action="hold", priority_score=70, weight_mode="manual",
            calc_batch_id=f"unrelated-score-{index:02d}",
            created_at=stamp, published_at=stamp,
        ))
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "public-lifecycle.duckdb")
    warehouse.upsert_records("factor_targets", [{
        "symbol": "000001", "signal_date": old_date,
        "entry_date": old_date + timedelta(days=1), "exit_date": old_date + timedelta(days=6),
        "target_code": "target_5d_return", "target_value": .01,
        "is_tradable": True, "invalid_reason": None,
        "calc_batch_id": f"unrelated-target-{index:02d}",
        "created_at": old_time + timedelta(days=index),
    } for index in range(32)])

    def all_old_targets():
        with warehouse.connection(read_only=True) as conn:
            return conn.execute("SELECT * FROM factor_targets WHERE calc_batch_id LIKE 'unrelated-target-%' ORDER BY calc_batch_id").fetchall()
    all_targets_before=all_old_targets()
    assert len(all_targets_before)==32
    before_history = client.get(f"/api/v1/scores/history/{symbol_ids[0]}?limit=100").json()
    assert len(before_history) == 32
    from sqlalchemy import text
    original_rows=[dict(r) for r in db_session.execute(text("SELECT * FROM scores ORDER BY id")).mappings()]
    before_runtime = client.get("/api/v1/factor-models/runtime").json()
    before_target, _, _ = warehouse.get_target_panel("unrelated-target-00")
    assert len(before_target) == 1

    request = {
        "train_model": True, "factor_set_id": set_id,
        "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
        "window_days": 90, "validation_days": 25,
    }
    if materialize_scores is not None:
        request["materialize_scores"] = materialize_scores
    submitted = client.post("/api/v1/scoring/tasks", json=request)
    assert submitted.status_code == 200, submitted.text
    task = client.get(f"/api/v1/scoring/tasks/{submitted.json()['id']}").json()
    assert task["status"] == "done", task
    result = task["result"]
    assert result["model"]["status"] == "validated", result
    assert result["completion_status"] == "AWAITING_MODEL_ACTIVATION"
    assert result["trained_model_auto_activated"] is False
    assert client.get("/api/v1/factor-models/runtime").json() == before_runtime
    assert [dict(r) for r in db_session.execute(text("SELECT * FROM scores ORDER BY id")).mappings()]==original_rows
    model = client.get(f"/api/v1/factor-models/{result['model']['model_run_id']}").json()
    assert model["status"] == "validated" and model["activated_at"] is None
    assert client.get(f"/api/v1/scores/history/{symbol_ids[0]}?limit=100").json() == before_history
    after_target, _, _ = warehouse.get_target_panel("unrelated-target-00")
    assert after_target.equals(before_target)
    assert result["batch_retention"] == {
        "status": "SKIPPED", "reason": "TRAINING_ONLY_NON_PUBLISHING",
    }

    # A routine refresh is not explicit archival authorization. It must retain
    # all 32 historical batches, including unreferenced score/target rows.
    refreshed = client.post("/api/v1/scoring/tasks", json={
        "train_model": False, "materialize_scores": False,
        "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
    })
    assert refreshed.status_code == 200, refreshed.text
    refreshed_task = client.get(f"/api/v1/scoring/tasks/{refreshed.json()['id']}").json()
    assert refreshed_task["status"] == "done", refreshed_task
    retention = refreshed_task["result"]["batch_retention"]
    assert retention == {"status":"SKIPPED","reason":"HISTORY_PRESERVATION_REQUIRED"}
    history = client.get(f"/api/v1/scores/history/{symbol_ids[0]}?limit=100").json()
    assert history == before_history
    removed_target, _, _ = warehouse.get_target_panel("unrelated-target-00")
    assert removed_target.equals(before_target)
    assert all_old_targets()==all_targets_before
    assert client.get("/api/v1/factor-models/runtime").json() == before_runtime
    assert [dict(r) for r in db_session.execute(text("SELECT * FROM scores ORDER BY id")).mappings()]==original_rows
