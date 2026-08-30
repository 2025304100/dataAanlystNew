"""Stage 5 task delivery and resume idempotency contracts."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

from app.models.async_task import AsyncTaskRecord
from app.services import async_tasks
from app.services import task_state_machine as tsm


def _task(db_session, *, status="interrupted", payload=None):
    row = AsyncTaskRecord(
        id="stage5-resume-contract",
        task_type="market_data_sync",
        status=status,
        stage="sync",
        percent=40,
        message="",
        total=10,
        batch_recovery_json="[]",
        payload_json="{}",
        is_terminal_locked=0,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_duplicate_task_delivery_reuses_the_original_task(db_session):
    payload = {"portfolio_id": 42, "trade_date": "2026-08-22", "symbols": [1, 2]}
    first = async_tasks.create_async_task(
        "market_data_sync",
        payload,
    )
    deliveries = [async_tasks.create_async_task("market_data_sync", payload) for _ in range(2)]

    assert [task.id for task in deliveries] == [first.id, first.id]
    rows = db_session.query(AsyncTaskRecord).filter_by(task_type="market_data_sync").all()
    assert len(rows) == 1


def test_duplicate_delivery_reuses_a_terminal_task(db_session):
    payload = {"portfolio_id": 42, "trade_date": "2026-08-22", "symbols": [1, 2]}
    first = async_tasks.create_async_task("market_data_sync", payload)
    persisted = db_session.get(AsyncTaskRecord, first.id)
    persisted.status = "done"
    persisted.stage = "done"
    persisted.is_terminal_locked = 1
    db_session.commit()

    replay = async_tasks.create_async_task("market_data_sync", payload)

    assert replay.id == first.id
    assert replay.status == "done"
    assert db_session.query(AsyncTaskRecord).filter_by(task_type="market_data_sync").count() == 1


def test_force_new_delivery_creates_a_distinct_task(db_session):
    payload = {"portfolio_id": 42, "trade_date": "2026-08-22", "symbols": [1, 2]}
    first = async_tasks.create_async_task("market_data_sync", payload)
    replay = async_tasks.create_async_task("market_data_sync", payload, force_new=True)

    assert replay.id != first.id
    assert replay.idempotency_key != first.idempotency_key
    assert db_session.query(AsyncTaskRecord).filter_by(task_type="market_data_sync").count() == 2


def test_resume_task_starts_only_one_worker_for_duplicate_resume_delivery(db_session):
    task = _task(db_session)
    started: list[str] = []

    def fake_start(task_id, worker):
        started.append(task_id)

    with patch.object(async_tasks, "_start_worker", side_effect=fake_start):
        first = tsm.resume_task(task.id, lambda task_id, batch_recovery=None: None)
        assert first.status == "running"
        try:
            tsm.resume_task(task.id, lambda task_id, batch_recovery=None: None)
        except ValueError as exc:
            assert "not resumable" in str(exc)
        else:
            raise AssertionError("duplicate resume delivery must not start another worker")

    assert started == [task.id]


def test_cancelled_task_cannot_be_resumed_by_a_duplicate_delivery(db_session):
    task = _task(db_session)
    started: list[str] = []

    with patch.object(async_tasks, "_start_worker", side_effect=lambda task_id, worker: started.append(task_id)):
        tsm.resume_task(task.id, lambda task_id, batch_recovery=None: None)
        cancelled = tsm.cancel_task_with_cleanup(task.id)
        assert cancelled.status == "cancelled"
        try:
            tsm.resume_task(task.id, lambda task_id, batch_recovery=None: None)
        except ValueError as exc:
            assert "not resumable" in str(exc)
        else:
            raise AssertionError("cancelled task must never be resumed")

    db_session.expire_all()
    persisted = db_session.get(AsyncTaskRecord, task.id)
    assert persisted.status == "cancelled"
    assert started == [task.id]


def test_worker_crash_is_terminal_and_not_resumable(db_session):
    task = _task(db_session, status="running")
    crashed = threading.Event()

    def worker(_task_id):
        crashed.set()
        raise RuntimeError("injected worker failure")

    async_tasks._start_worker(task.id, worker)
    assert crashed.wait(2), "worker did not start"

    deadline = time.monotonic() + 2
    persisted = None
    while time.monotonic() < deadline:
        db_session.expire_all()
        persisted = db_session.get(AsyncTaskRecord, task.id)
        if persisted is not None and persisted.status == "failed":
            break
        time.sleep(0.02)

    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.stage == "failed"
    assert int(getattr(persisted, "is_terminal_locked", 0) or 0) == 1
    assert persisted.correlation_id
    errors = async_tasks._json_loads(persisted.errors_json, [])
    assert any(item.get("code") == "app.worker.crashed" or item.get("code") == "eval.worker.crashed" for item in errors)

    with patch.object(async_tasks, "_start_worker") as start_worker:
        try:
            tsm.resume_task(task.id, lambda task_id, batch_recovery=None: None)
        except ValueError as exc:
            assert "not resumable" in str(exc)
        else:
            raise AssertionError("failed worker task must not be resumable")
        start_worker.assert_not_called()
