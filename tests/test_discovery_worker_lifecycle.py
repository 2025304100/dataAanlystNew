"""Public discovery lifecycle with delayed real workers and isolated SQLite."""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.schemas.discovery import DiscoveryTaskCreate
from app.services import discovery_tasks

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
        if thread.name.startswith("discovery-watchdog-"):
            real_start(thread)
        else:
            pending.append(thread)

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
