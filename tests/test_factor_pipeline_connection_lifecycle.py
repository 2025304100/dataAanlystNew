"""Public training must not carry an idle SQL lease across warehouse work.

SQLite/DuckDB are temporary. The SQL adapter simulates the observed MySQL
idle-connection expiry without waiting eighteen minutes or hiding any errors.
"""
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from tests.test_whitebox_factor_public_lifecycle_e2e import (
    lifecycle_client, _draft, _seed_local_market,
)


def test_public_training_reacquires_metadata_connection_after_long_warehouse_work(
    lifecycle_client, db_session, tmp_path, monkeypatch,
):
    from app.services.factors.store import FactorWarehouse

    _draft(db_session)
    _path, days, _symbols, _watchlist = _seed_local_market(db_session, tmp_path)
    client = lifecycle_client
    created = client.post("/api/v1/factor-sets", json={"name": "Idle SQL lease isolation"})
    assert created.status_code == 200, created.text
    set_id = created.json()["id"]
    assert client.post(f"/api/v1/factor-sets/{set_id}/members", json={
        "factor_code": "ep_ttm", "factor_version": 2,
        "role": "feature", "missing_policy": "exclude",
    }).status_code == 200
    assert client.post(f"/api/v1/scoring/factorsets/{set_id}/freeze", json={}).status_code == 200
    db_session.rollback()

    engine = db_session.get_bind()
    active, expired, warehouse_phases = set(), set(), []
    serial = [0]

    def checkout(_connection, record, _proxy):
        serial[0] += 1
        record.info["qa_idle_lease"] = serial[0]
        active.add(serial[0])

    def checkin(_connection, record):
        active.discard(record.info.get("qa_idle_lease"))

    def before_sql(connection, _cursor, statement, parameters, _context, _many):
        if (connection.info.get("qa_idle_lease") in expired
                and ("FROM factor_sets" in statement or "FROM factor_versions" in statement)):
            raise OperationalError(statement, parameters,
                RuntimeError("2006 MySQL server has gone away: idle warehouse lease"))

    original_connection = FactorWarehouse.connection

    class WarehouseAdapter:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, *args, **kwargs):
            if "WITH valuation_dedup AS" in statement or "entry_calendar" in statement:
                warehouse_phases.append("targets" if "entry_calendar" in statement else "factors")
                expired.update(active)
            return self.connection.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def warehouse_connection(warehouse, **kwargs):
        with original_connection(warehouse, **kwargs) as connection:
            yield WarehouseAdapter(connection)

    for name, callback in (("checkout", checkout), ("checkin", checkin),
                           ("before_cursor_execute", before_sql)):
        event.listen(engine, name, callback)
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
        assert task["result"]["completion_status"] == "AWAITING_MODEL_ACTIVATION"
        assert task["result"]["model"]["status"] == "validated"
        assert warehouse_phases == ["factors", "targets"]
        runtime = client.get("/api/v1/scoring/overview").json()
        assert runtime["runtime_mode"] == "manual" and runtime["runtime_active_model_id"] is None
    finally:
        for name, callback in (("checkout", checkout), ("checkin", checkin),
                               ("before_cursor_execute", before_sql)):
            event.remove(engine, name, callback)
