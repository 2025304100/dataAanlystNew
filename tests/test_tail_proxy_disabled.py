"""Retiring the proxy blocks old entry points before any I/O or task creation."""
import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.scheduled_task import ScheduledTask
from app.models.factor import Factor
from app.services import external_data_sync_task as external, scheduled_tasks
from app.services.feature_policy import TAIL_PROXY_DISABLED_REASON
from app.services.factors.definitions import FACTOR_BY_CODE, seed_factor_definitions
from app.services.factors.factor_compiler import compile_formula
from app.services.factors.formula_catalog import build_formula_catalog
from app.services.factors.factor_registry import list_factors

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("entry", ["sync", "import", "task", "external", "plan", "bulk"])
def test_retired_entry_points_reject_before_database_or_network(entry, monkeypatch):
    from app.services import tail_proxy_data, tail_proxy_import, tail_proxy_task

    database = Mock()
    network = Mock(side_effect=AssertionError("must not fetch minute bars"))
    monkeypatch.setattr(tail_proxy_data, "_fetch_minute_frame", network)
    create_task = Mock(side_effect=AssertionError("must not create a task"))
    monkeypatch.setattr(tail_proxy_task, "create_async_task", create_task)
    calls = {
        "sync": lambda: tail_proxy_data.sync_tail_proxy_snapshots(database),
        "import": lambda: tail_proxy_import.import_tail_minute_csv(database, csv_text="anything", filename="tail.csv"),
        "task": lambda: tail_proxy_task.create_tail_proxy_task(),
        "external": lambda: external.start_external_data_sync("tail_proxy", {}),
        "plan": lambda: external.build_external_sync_plan("tail_proxy", {}),
        "bulk": lambda: external._run_bulk_sync(database, "legacy", "tail_proxy", {}),
    }
    with pytest.raises(ValueError, match="尾盘代理已停用"):
        calls[entry]()
    assert not database.mock_calls
    network.assert_not_called()
    create_task.assert_not_called()


@pytest.mark.parametrize("path,payload", [
    ("/external-data/tail-proxy/sync", None),
    ("/external-data/tail-proxy/import", {"csv_text": "symbol,timestamp"}),
    ("/external-data/sync-tasks", {"dataset": "tail_proxy"}),
    ("/external-data/sync-plans/preview", {"dataset": "tail_proxy"}),
])
def test_old_http_clients_cannot_restart_proxy(db_session, path, payload):
    from app.api.routes.external_data import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    response = TestClient(app).post(path, json=payload)
    assert response.status_code in (400, 410)
    assert "尾盘代理已停用" in response.json()["detail"]


@pytest.mark.parametrize("task_type,payload", [
    ("tail_proxy_snapshot", {}),
    ("external_data_sync", {"dataset": "tail_proxy"}),
])
def test_legacy_schedules_are_disabled_hidden_and_cannot_dispatch(db_session, task_type, payload):
    item = ScheduledTask(name="old tail schedule", task_type=task_type, frequency="daily",
                         time_of_day="15:10", timezone="Asia/Shanghai", weekdays_json="[]",
                         payload_json=json.dumps(payload), enabled=1, next_run_at=datetime(2026, 10, 19))
    db_session.add(item)
    db_session.commit()
    assert scheduled_tasks.disable_retired_schedules(db_session) == 1
    assert item.enabled == 0 and item.next_run_at is None
    assert scheduled_tasks.disable_retired_schedules(db_session) == 0
    with pytest.raises(ValueError, match="尾盘代理已停用"):
        scheduled_tasks.validate_task_payload(task_type, payload)
    with pytest.raises(ValueError, match="尾盘代理已停用"):
        scheduled_tasks._dispatch_task(item)
    from app.api.routes.scheduled_tasks import list_scheduled_tasks
    assert list_scheduled_tasks(db_session) == []
    assert "tail_proxy_snapshot" not in scheduled_tasks.TASK_DEFINITIONS
    assert all(row["task_type"] != "tail_proxy_snapshot" for row in scheduled_tasks.DEFAULT_SCHEDULES)


@pytest.mark.parametrize("strict", [True, False])
def test_proxy_is_absent_from_catalog_and_cannot_compile_even_as_a_parameter(strict):
    assert "proxy_score" not in {field["key"] for field in build_formula_catalog()["fields"]}
    result = compile_formula(formula="proxy_score + close", params={"proxy_score": 1}, strict_fields=strict)
    assert not result.success
    assert "field_disabled" in {error.error_code for error in result.errors}


def test_old_factor_is_retired_without_deleting_history_or_reseeding(db_session):
    factor = Factor(code="tail_accumulation_proxy", name="old proxy", category="capital_flow",
                    direction="higher_better", status="active", is_active=1)
    db_session.add(factor)
    db_session.flush()
    seed_factor_definitions(db_session)
    assert "tail_accumulation_proxy" not in FACTOR_BY_CODE
    assert factor.is_active == 0 and factor.lifecycle_status == "deprecated"
    rows, total = list_factors(db_session, search="tail_accumulation_proxy")
    assert rows == [] and total == 0
    assert db_session.get(Factor, factor.id) is factor


@pytest.mark.parametrize("panel", [True, False])
def test_saved_execution_plans_cannot_read_retired_minute_history(panel):
    from app.services.factors.factor_executor import FactorExecutor
    warehouse = Mock()
    executor = FactorExecutor(warehouse)
    plan = SimpleNamespace(data_dependencies={"fields": ["proxy_score"], "source_tables": ["raw_tail_proxy"]})
    if panel:
        frame, errors = executor._read_source_data_panel(plan, start_date=date(2026, 10, 16), end_date=date(2026, 10, 16))
    else:
        frame, errors = executor._read_source_data(plan)
    assert frame.empty and errors[0]["category"] == "feature_disabled"
    assert errors[0]["message"] == TAIL_PROXY_DISABLED_REASON
    assert not warehouse.mock_calls


def test_overview_capabilities_and_coverage_have_only_supported_datasets(db_session):
    overview = external.get_external_data_overview(db_session)
    assert len(overview["datasets"]) == 6
    assert all(item["dataset"] != "tail_proxy" for item in overview["datasets"])
    assert "tail_proxy" not in external.get_external_sync_capabilities()
    assert all(item["dataset"] != "tail_proxy" for item in external.get_external_data_coverage(db_session)["datasets"])


def test_discovery_does_not_enqueue_or_report_missing_tail_proxy(monkeypatch):
    from app.services.discovery_data_prep import _invoke_external_data_sync
    for target in (
        "app.services.financial_report_task.create_financial_report_task",
        "app.services.macro_update_task.create_macro_update_task",
        "app.services.hot_rank_task.create_hot_rank_task",
        "app.services.lhb_institution_task.create_lhb_institution_task",
    ):
        monkeypatch.setattr(target, Mock())
    create_tail = Mock(side_effect=AssertionError("retired task must not be queued"))
    monkeypatch.setattr("app.services.tail_proxy_task.create_tail_proxy_task", create_tail)
    assert _invoke_external_data_sync() == []
    create_tail.assert_not_called()


def test_legacy_mirror_flag_does_not_import_retired_rows_or_break_other_inputs(db_session, tmp_path):
    from app.services.factors.data_sync import mirror_factor_inputs
    from app.services.factors.store import FactorWarehouse
    from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
    db_session.add(TailAccumulationSnapshot(symbol="000001", trade_date=date(2026, 10, 16),
        minute_count=240, tail_minute_count=30, day_amount=20000, tail_amount=3000,
        tail_amount_share=.15, tail_activity_ratio=1, tail_return=.01, close_location=.8,
        proxy_score=1, source="legacy"))
    db_session.flush()
    warehouse = FactorWarehouse(tmp_path / "disabled-tail.duckdb")
    result = mirror_factor_inputs(db_session, warehouse=warehouse, include_tail_proxy=True,
        include_valuations=True, include_financial_reports=False, include_fund_flows=False,
        include_sentiment=False, include_etf_indicators=False, include_macro=False)
    assert result.tail_proxy_rows == 0 and result.tail_proxy_watermark == 0
    assert db_session.query(TailAccumulationSnapshot).count() == 1
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_tail_proxy").fetchone()[0] == 0
