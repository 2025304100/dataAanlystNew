"""A selected one-factor training job must not refresh unrelated datasets."""
from contextlib import contextmanager

from sqlalchemy import event

from tests.test_turnover_v3_public_warehouse_lifecycle import (
    turnover_client, FORMULA, _seed_turnover_market,
)
import pytest

pytestmark = pytest.mark.whitebox


def test_public_guarded_turnover_training_reads_only_its_frozen_input_dependencies(
    turnover_client, db_session, tmp_path, monkeypatch,
):
    from app.services.factors.store import FactorWarehouse
    from app.services.factors.definitions import seed_factor_definitions

    seed_factor_definitions(db_session)
    db_session.commit()
    days, _symbols, _watchlist = _seed_turnover_market(db_session, tmp_path)
    client = turnover_client
    version = client.post("/api/v1/factors/turnover_z20/versions", json={
        "formula_expr": FORMULA, "params": {}, "direction": "nonlinear",
        "source_mapping": {"table": "raw_daily_bars", "field": "turnover_rate"},
        "postprocess": {"winsorize": {"method": "mad", "mad_multiplier": 3.0},
                        "zscore": {"ddof": 0}, "missing_policy": "exclude"},
    })
    assert version.status_code == 201, version.text
    created = client.post("/api/v1/factor-sets", json={"name": "Only requested turnover"})
    assert created.status_code == 200, created.text
    set_id = created.json()["id"]
    assert client.post(f"/api/v1/factor-sets/{set_id}/members", json={
        "factor_code": "turnover_z20", "factor_version": version.json()["version"],
        "role": "feature", "missing_policy": "exclude",
    }).status_code == 200
    assert client.post(f"/api/v1/scoring/factorsets/{set_id}/freeze", json={}).status_code == 200
    db_session.rollback()
    engine = db_session.get_bind()
    unrelated_sql, unrelated_warehouse = [], []

    def before_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if any(f"FROM {table}" in statement for table in (
                "stock_valuations", "stock_financial_reports", "capital_flows",
                "lhb_institution_trades", "stock_hot_rank_snapshots",
                "tail_accumulation_snapshots", "etf_indicators", "macro_indicator_values")):
            unrelated_sql.append(statement)
            raise AssertionError("A turnover-only training job read an unrelated SQL input")

    original_connection = FactorWarehouse.connection

    class WarehouseAdapter:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, *args, **kwargs):
            if "WITH valuation_dedup AS" in statement:
                unrelated_warehouse.append(statement)
                raise AssertionError("A turnover-only job calculated the unrelated native batch")
            return self.connection.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def warehouse_connection(warehouse, **kwargs):
        with original_connection(warehouse, **kwargs) as connection:
            yield WarehouseAdapter(connection)

    event.listen(engine, "before_cursor_execute", before_sql)
    monkeypatch.setattr(FactorWarehouse, "connection", warehouse_connection)
    try:
        submitted = client.post("/api/v1/scoring/tasks", json={
            "train_model": True, "materialize_scores": False, "factor_set_id": set_id,
            "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
            "window_days": 90, "validation_days": 25,
        })
        assert submitted.status_code == 200, submitted.text
        task = client.get(f"/api/v1/scoring/tasks/{submitted.json()['id']}").json()
        assert task["status"] == "done", task
        result = task["result"]
        assert result["completion_status"] == "AWAITING_MODEL_ACTIVATION"
        assert result["model"]["status"] == "validated"
        assert result["input_mirror"]["rows_written"] == 0
        assert result["input_mirror"]["source_tables"] == ["raw_daily_bars"]
        assert result["factors"]["rows_written"] == len(days) * 24
        assert set(result["factors"]["coverage_by_factor"]) == {"turnover_z20"}
        assert not unrelated_sql and not unrelated_warehouse
        detail = client.get(f"/api/v1/scoring/models/{result['model']['model_run_id']}").json()
        assert [(row["factor_code"], row["factor_version"]) for row in detail["factors"]] == [
            ("turnover_z20", version.json()["version"])]
    finally:
        event.remove(engine, "before_cursor_execute", before_sql)


def test_public_general_refresh_keeps_default_eight_factor_batch(
    turnover_client, db_session, tmp_path,
):
    from app.services.factors.definitions import seed_factor_definitions

    seed_factor_definitions(db_session)
    db_session.commit()
    days, _symbols, _watchlist = _seed_turnover_market(db_session, tmp_path)
    submitted = turnover_client.post("/api/v1/scoring/tasks", json={
        "train_model": False, "materialize_scores": False,
        "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
    })
    assert submitted.status_code == 200, submitted.text
    task = turnover_client.get(f"/api/v1/scoring/tasks/{submitted.json()['id']}").json()
    assert task["status"] == "done", task
    result = task["result"]
    assert result["completion_status"] == "COMPLETED"
    assert result["input_mirror"]["valuation_rows"] == 24 * len(days)
    assert result["factors"]["rows_written"] == 8 * 24 * len(days)
    assert set(result["factors"]["coverage_by_factor"]) == {
        "ep_ttm", "negative_pb", "roe_yoy_growth", "main_inflow_5d_ratio",
        "lhb_institution_net_ratio", "turnover_z20", "hot_rank_attention",
        "tail_accumulation_proxy",
    }
