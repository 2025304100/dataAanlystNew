from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.scheduled_task import ScheduledTask, ScheduledTaskRun
from app.schemas.scheduled_task import ScheduledTaskCreate, ScheduledTaskUpdate
from app.services import scheduled_tasks


def test_calculate_next_daily_and_weekly_run_in_configured_timezone():
    after = datetime(2026, 7, 13, 9, 0)  # 17:00 Asia/Shanghai
    daily = scheduled_tasks.calculate_next_run(
        frequency="daily",
        time_of_day="18:00",
        weekdays=[],
        interval_minutes=None,
        timezone_name="Asia/Shanghai",
        after_utc=after,
    )
    weekly = scheduled_tasks.calculate_next_run(
        frequency="weekly",
        time_of_day="18:30",
        weekdays=[4],
        interval_minutes=None,
        timezone_name="Asia/Shanghai",
        after_utc=after,
    )

    assert daily == datetime(2026, 7, 13, 10, 0)
    assert weekly == datetime(2026, 7, 17, 10, 30)


def test_seed_default_schedules_is_idempotent(db_session):
    assert scheduled_tasks.seed_default_schedules(db_session) == 9
    db_session.commit()
    assert scheduled_tasks.seed_default_schedules(db_session) == 0
    rows = db_session.query(ScheduledTask).all()
    assert len(rows) == 9
    enabled = [item for item in rows if item.enabled]
    assert [item.task_type for item in enabled] == ["universe_incremental_sync"]
    assert enabled[0].next_run_at is not None


def test_deleted_or_renamed_defaults_are_not_reseeded(db_session):
    assert scheduled_tasks.seed_default_schedules(db_session) == 9
    db_session.commit()
    item = db_session.query(ScheduledTask).filter_by(name="每日宏观数据更新").one()
    scheduled_tasks.delete_schedule(db_session, item.id)

    assert scheduled_tasks.seed_default_schedules(db_session) == 0
    assert db_session.query(ScheduledTask).filter_by(name="每日宏观数据更新").count() == 0


def test_schedule_create_update_and_delete(db_session):
    item = scheduled_tasks.create_schedule(
        db_session,
        ScheduledTaskCreate(
            name="Macro every hour",
            task_type="macro_update",
            frequency="interval",
            time_of_day=None,
            interval_minutes=60,
            payload={"region": "all"},
        ),
    )
    assert item.next_run_at is not None

    item = scheduled_tasks.update_schedule(
        db_session,
        item.id,
        ScheduledTaskUpdate(enabled=False, name="Paused macro"),
    )
    assert item.name == "Paused macro"
    assert item.enabled == 0
    assert item.next_run_at is None

    scheduled_tasks.delete_schedule(db_session, item.id)
    assert db_session.get(ScheduledTask, item.id) is None


def test_manual_execute_records_auditable_run(db_session, monkeypatch):
    item = scheduled_tasks.create_schedule(
        db_session,
        ScheduledTaskCreate(
            name="Manual macro",
            task_type="macro_update",
            frequency="daily",
            time_of_day="17:30",
            payload={"region": "all"},
            enabled=False,
        ),
    )
    monkeypatch.setattr(
        scheduled_tasks,
        "_dispatch_task",
        lambda schedule: (
            "async",
            SimpleNamespace(id="task-123", status="queued", message="queued"),
        ),
    )

    run = scheduled_tasks.execute_schedule(
        db_session,
        item.id,
        trigger_source="manual",
    )
    db_session.refresh(item)

    assert run.task_id == "task-123"
    assert run.trigger_source == "manual"
    assert item.last_task_id == "task-123"
    assert item.last_error is None
    assert db_session.query(ScheduledTaskRun).count() == 1


def test_manual_execute_accepts_dict_task_result(db_session, monkeypatch):
    item = scheduled_tasks.create_schedule(
        db_session,
        ScheduledTaskCreate(
            name="Factor task",
            task_type="factor_pipeline",
            frequency="daily",
            time_of_day="18:10",
            payload={
                "train_model": False,
                "materialize_scores": True,
                "window_days": 250,
                "validation_days": 50,
            },
            enabled=False,
        ),
    )
    monkeypatch.setattr(
        scheduled_tasks,
        "_dispatch_task",
        lambda schedule: (
            "async",
            {"id": "factor-123", "status": "queued", "message": "queued"},
        ),
    )

    run = scheduled_tasks.execute_schedule(
        db_session,
        item.id,
        trigger_source="manual",
    )

    assert run.task_id == "factor-123"
    assert run.status == "queued"


def test_due_schedule_is_claimed_once(db_session, monkeypatch):
    item = scheduled_tasks.create_schedule(
        db_session,
        ScheduledTaskCreate(
            name="Due macro",
            task_type="macro_update",
            frequency="daily",
            time_of_day="17:30",
            payload={"region": "all"},
        ),
    )
    item.next_run_at = datetime(2020, 1, 1)
    db_session.commit()
    calls = []
    monkeypatch.setattr(
        scheduled_tasks,
        "execute_schedule",
        lambda db, schedule_id, trigger_source: calls.append(
            (schedule_id, trigger_source)
        ),
    )

    assert scheduled_tasks.run_due_schedules() == 1
    assert scheduled_tasks.run_due_schedules() == 0
    assert calls == [(item.id, "scheduled")]


def test_invalid_task_payload_is_rejected_when_schedule_is_saved(db_session):
    try:
        scheduled_tasks.create_schedule(
            db_session,
            ScheduledTaskCreate(
                name="Invalid factor task",
                task_type="factor_pipeline",
                frequency="daily",
                time_of_day="18:10",
                payload={"window_days": 60, "validation_days": 60},
            ),
        )
    except ValueError as exc:
        assert "validation_days" in str(exc)
    else:
        raise AssertionError("invalid factor payload must be rejected")


def test_scheduled_task_routes_are_registered():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/v1/scheduled-tasks" in paths
    assert "/api/v1/scheduled-tasks/{schedule_id}/run" in paths
    assert "/api/v1/scheduled-tasks/runs" in paths


def test_scheduled_task_api_crud_and_manual_run(db_session, monkeypatch):
    from app.main import app

    def override_get_db():
        yield db_session

    monkeypatch.setattr(
        scheduled_tasks,
        "_dispatch_task",
        lambda schedule: (
            "async",
            SimpleNamespace(id="api-task-123", status="queued", message="queued"),
        ),
    )
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        definitions = client.get("/api/v1/scheduled-tasks/definitions")
        assert definitions.status_code == 200
        assert {item["task_type"] for item in definitions.json()} >= {
            "universe_incremental_sync",
            "macro_update",
            "factor_pipeline",
            "hot_rank_snapshot",
            "tail_proxy_snapshot",
            "lhb_institution_sync",
            "financial_report_sync",
            "discovery_mining",
        }

        created = client.post(
            "/api/v1/scheduled-tasks",
            json={
                "name": "API macro task",
                "task_type": "macro_update",
                "frequency": "daily",
                "time_of_day": "17:30",
                "timezone": "Asia/Shanghai",
                "payload": {"region": "all"},
                "enabled": True,
            },
        )
        assert created.status_code == 200
        schedule_id = created.json()["id"]
        assert created.json()["next_run_at"] is not None

        listed = client.get("/api/v1/scheduled-tasks")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [schedule_id]

        disabled = client.patch(
            f"/api/v1/scheduled-tasks/{schedule_id}",
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["next_run_at"] is None

        executed = client.post(f"/api/v1/scheduled-tasks/{schedule_id}/run")
        assert executed.status_code == 200
        assert executed.json()["task_id"] == "api-task-123"
        assert executed.json()["trigger_source"] == "manual"

        runs = client.get("/api/v1/scheduled-tasks/runs?limit=10")
        assert runs.status_code == 200
        assert runs.json()[0]["schedule_id"] == schedule_id

        deleted = client.delete(f"/api/v1/scheduled-tasks/{schedule_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"status": "deleted", "id": schedule_id}
        assert client.get(f"/api/v1/scheduled-tasks/{schedule_id}").status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)
