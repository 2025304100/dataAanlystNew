"""Public discovery lifecycle with delayed real workers and isolated SQLite."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.schemas.discovery import DiscoveryTaskCreate
from app.services import discovery_tasks
from app.models.discovery import DiscoveryTaskRecord
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes.discovery import router

pytestmark = pytest.mark.whitebox


@pytest.fixture
def queued_workers(monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    class DelayedWorkers(list):
        pass
    pending = DelayedWorkers()
    real_start = threading.Thread.start
    pending.engine = engine
    pending.real_start = real_start

    def delayed_start(thread):
        if getattr(thread, "_target", None) is discovery_tasks._run_discovery_task:
            pending.append(thread)
        else:
            # Real HTTP clients need their AnyIO portal thread to run normally.
            real_start(thread)

    monkeypatch.setattr(discovery_tasks, "SessionLocal", factory)
    monkeypatch.setattr(threading.Thread, "start", delayed_start)
    yield pending
    engine.dispose()


def create_task():
    return discovery_tasks.create_discovery_task(DiscoveryTaskCreate(
        scope="cn-stock", include_news=False, use_cached_symbols_only=True,
        global_mode="cached"))


def test_cancelled_queued_task_cannot_be_revived_by_its_delayed_worker(queued_workers):
    task = create_task()
    cancelled = discovery_tasks.cancel_discovery_task(task["id"])
    assert cancelled["status"] == "cancelled"

    queued_workers.pop(0).run()

    after = discovery_tasks.get_discovery_task(task["id"])
    assert after["status"] == "cancelled"
    assert after["started_at"] is None


def public_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("stage", ["prepare", "sync", "scan", "news"])
def test_cancel_endpoint_preserves_the_real_business_stage(queued_workers, stage):
    task = create_task()
    with discovery_tasks.SessionLocal() as db:
        row = db.get(DiscoveryTaskRecord, task["id"])
        row.status, row.stage, row.total, row.processed = "running", stage, 10, 4
        db.commit()
    client = public_client()
    response = client.post(f'/discovery/tasks/{task["id"]}/cancel')
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["stage"] == stage
    assert response.json()["percent"] == 40
    with discovery_tasks.SessionLocal() as db:
        assert db.get(DiscoveryTaskRecord, task["id"]).percent == 40
    persisted = client.get(f'/discovery/tasks/{task["id"]}')
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["stage"] == stage


def test_real_empty_store_failure_reports_prepare_in_the_public_response(queued_workers):
    task = create_task()
    queued_workers.pop(0).run()
    response = public_client().get(f'/discovery/tasks/{task["id"]}')
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["stage"] == "prepare"
    assert response.json()["percent"] == 0


def test_pause_and_resume_preserve_the_last_real_business_stage(queued_workers):
    task = create_task()
    with discovery_tasks.SessionLocal() as db:
        row = db.get(DiscoveryTaskRecord, task["id"])
        row.status, row.stage = "running", "scan"
        db.commit()
    client = public_client()
    paused = client.post(f'/discovery/tasks/{task["id"]}/pause')
    assert paused.status_code == 200, paused.text
    assert paused.json()["stage"] == "scan"
    resumed = client.post(f'/discovery/tasks/{task["id"]}/resume')
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "queued"
    assert resumed.json()["stage"] == "scan"


def test_stale_task_failure_preserves_its_last_business_stage(queued_workers):
    task = create_task()
    with discovery_tasks.SessionLocal() as db:
        row = db.get(DiscoveryTaskRecord, task["id"])
        row.status, row.stage = "running", "news"
        row.updated_at = discovery_tasks._now() - discovery_tasks.STALE_RUNNING_DEADLINE - timedelta(minutes=1)
        db.commit()
    response = public_client().get(f'/discovery/tasks/{task["id"]}')
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["stage"] == "news"


def test_expired_paused_task_preserves_its_last_business_stage(queued_workers):
    task = create_task()
    with discovery_tasks.SessionLocal() as db:
        row = db.get(DiscoveryTaskRecord, task["id"])
        row.status, row.stage = "paused", "sync"
        row.paused_at = discovery_tasks._now() - discovery_tasks.RESUME_DEADLINE - timedelta(minutes=1)
        db.commit()
    replacement = create_task()
    assert replacement["id"] != task["id"]
    response = public_client().get(f'/discovery/tasks/{task["id"]}')
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "expired"
    assert response.json()["stage"] == "sync"


def test_retry_does_not_let_the_previous_worker_claim_the_new_attempt(queued_workers):
    task = create_task()
    old_worker = queued_workers.pop(0)
    discovery_tasks.cancel_discovery_task(task["id"])
    retried = discovery_tasks.retry_discovery_task(task["id"])
    assert retried["status"] == "queued"
    new_worker = queued_workers.pop(0)

    old_worker.run()

    after_old = discovery_tasks.get_discovery_task(task["id"])
    assert after_old["status"] == "queued"
    assert after_old["started_at"] is None
    new_worker.run()
    after_new = discovery_tasks.get_discovery_task(task["id"])
    assert after_new["status"] == "failed"  # The real empty-store failure.
    assert after_new["started_at"] is not None


@pytest.mark.parametrize("status,processed,total,expected", [
    ("cancelled", 0, 1, 0), ("cancelled", 0, 0, 0),
    ("failed", 0, 0, 0), ("expired", 0, 0, 0),
    ("partial", 5, 10, 50), ("partial", 0, 0, 0),
    ("paused", 1, 8, 13),
])
def test_terminal_api_percent_uses_counts_even_for_legacy_raw_hundred(queued_workers, status, processed, total, expected):
    task = create_task()
    with discovery_tasks.SessionLocal() as db:
        row = db.get(DiscoveryTaskRecord, task["id"])
        row.status, row.stage = status, "scan"
        row.processed, row.total, row.percent = processed, total, 100
        db.commit()
    response = public_client().get(f'/discovery/tasks/{task["id"]}')
    assert response.status_code == 200, response.text
    assert response.json()["processed"] == processed
    assert response.json()["total"] == total
    assert response.json()["percent"] == expected


def test_pause_and_resume_keep_a_delayed_old_worker_out_of_the_new_attempt(queued_workers):
    task = create_task()
    old_worker = queued_workers.pop(0)
    paused = discovery_tasks.pause_discovery_task(task["id"])
    assert paused["status"] == "paused"
    resumed = discovery_tasks.resume_discovery_task(task["id"])
    assert resumed["status"] == "queued"
    new_worker = queued_workers.pop(0)

    old_worker.run()

    assert discovery_tasks.get_discovery_task(task["id"])["status"] == "queued"
    new_worker.run()
    assert discovery_tasks.get_discovery_task(task["id"])["status"] == "failed"


def test_running_old_attempt_cannot_fail_or_complete_a_retried_task(queued_workers):
    from sqlalchemy import event
    entered = threading.Event()
    release = threading.Event()
    intercepted = False

    def wait_for_local_source(_conn, _cursor, statement, _params, _context, _many):
        nonlocal intercepted
        if not intercepted and "FROM universe_symbols" in statement:
            intercepted = True
            entered.set()
            if not release.wait(5):
                raise TimeoutError("The test did not release its isolated source query")

    task = create_task()
    old_worker = queued_workers.pop(0)
    event.listen(queued_workers.engine, "before_cursor_execute", wait_for_local_source)
    runner = threading.Thread(target=old_worker.run, daemon=True)
    queued_workers.real_start(runner)
    try:
        assert entered.wait(5)
        assert discovery_tasks.cancel_discovery_task(task["id"])["status"] == "cancelled"
        assert discovery_tasks.retry_discovery_task(task["id"])["status"] == "queued"
    finally:
        release.set()
        runner.join(5)
        event.remove(queued_workers.engine, "before_cursor_execute", wait_for_local_source)
    assert not runner.is_alive()
    assert discovery_tasks.get_discovery_task(task["id"])["status"] == "queued"
    queued_workers.pop(0).run()
    assert discovery_tasks.get_discovery_task(task["id"])["status"] == "failed"
