"""Shared writer lifecycle through mirror/mining submission and patrol entry points.

The mirror function and mining stages are injected public test seams. All task
state and locks live in the fixture's isolated SQLite database.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.models.task_lock import TaskLock
from app.services import async_tasks as AT
from app.services import task_state_machine as TSM
from app.services.factors import mirror_task as MT
from app.services.factors.mining import stall_watchdog as WD
from app.services.factors.mining import task_lock as TL
from app.services.factors.mining import task_runner as TR
from app.services.factors.mining import service as MS

pytestmark = pytest.mark.whitebox


@pytest.fixture
def writer_env(db_session, monkeypatch):
    import app.db.session as session_mod

    monkeypatch.setattr(session_mod, "_cp_factory", None)
    monkeypatch.setattr(session_mod, "_cp_engine", None)
    release = threading.Event()
    entered = threading.Event()

    def mirror(_db, **_kwargs):
        entered.set()
        assert release.wait(10), "test did not release the isolated mirror"
        return type("Result", (), {"to_dict": lambda self: {
            "rows_written": 1, "batch_id": "isolated-shared-writer",
        }})()

    factory = sessionmaker(bind=db_session.get_bind())
    yield factory, mirror, entered, release
    release.set()
    AT.request_all_workers_stop()
    AT.wait_workers_stopped(timeout_seconds=10)
    AT.WORKER_STOP_EVENT.clear()


def test_patrol_keeps_running_mirror_write_lock(writer_env):
    factory, mirror, entered, _release = writer_env
    task = MT.create_mirror_task(
        preset="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        mirror_func=mirror, skip_disk_check=True,
    )
    assert entered.wait(5)

    WD.watchdog_once(session_factory=factory)
    with factory() as db:
        progress = MT.get_mirror_task_progress(db, task["task_id"])
        locks = TL.get_lock_status(db)
    assert progress["status"] == "running"
    assert locks.duckdb_write["busy"] is True
    assert locks.duckdb_write["taskId"] == task["task_id"]
    with pytest.raises(RuntimeError, match="DUCKDB_WRITE_BUSY"):
        MT.create_mirror_task(
            preset="custom", start_date=date(2026, 1, 3), end_date=date(2026, 1, 4),
            mirror_func=mirror, skip_disk_check=True,
        )


def _wait_done(task_id: str) -> None:
    until = time.monotonic() + 5
    while time.monotonic() < until:
        task = AT.get_async_task(task_id)
        if task.status in {"done", "failed", "cancelled"}:
            assert task.status == "done", task.message
            return
        time.sleep(0.025)
    raise AssertionError(f"task did not finish: {AT.get_async_task(task_id).status}")


def test_mirror_completion_starts_queued_mining_with_its_submitted_stages(writer_env):
    factory, mirror, entered, release = writer_env
    MT.create_mirror_task(
        preset="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        mirror_func=mirror, skip_disk_check=True,
    )
    assert entered.wait(5)
    mined = threading.Event()

    def stages(ctx):
        assert ctx.holds_duckdb_write
        mined.set()
        return {"message_zh": "isolated submitted stages finished"}

    queued = TR.submit_mining_run(
        {"run_id": "queued-after-mirror"}, run_id="queued-after-mirror",
        stage_runner=stages,
    )
    assert not queued.started
    assert queued.queue_position == 1
    assert not mined.is_set()

    release.set()
    assert mined.wait(3), "promoted mining task was never started"
    _wait_done(queued.task_id)
    assert AT.get_async_task(queued.task_id).message == "isolated submitted stages finished"


def test_watchdog_releasing_terminal_mirror_starts_queued_mining(writer_env):
    factory, _mirror, _entered, _release = writer_env
    # An interrupted finalizer can leave a terminal async owner with its lock.
    owner = AT.create_async_task(MT.TASK_TYPE, {"orphan_fixture": True},
                                 use_control_plane=True)
    TSM.transition(owner.id, "running")
    TSM.transition(owner.id, "failed")
    with factory() as db:
        TL.acquire_write_slot(db, task_id=owner.id, run_id=owner.id)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        MS.create_run(
            db, run_id="queued-after-orphan", candidate_pool_snapshot_id="fixture-snapshot",
            data_cutoff_at=now, start_date=now, end_date=now,
        )
    mined = threading.Event()

    def stages(_ctx):
        mined.set()
        return {"message_zh": "watchdog resumed queued mining"}

    queued = TR.submit_mining_run(
        {"run_id": "queued-after-orphan"}, run_id="queued-after-orphan",
        stage_runner=stages,
    )
    assert queued.queue_position == 1
    WD.watchdog_once(session_factory=factory)

    assert mined.wait(3), "patrol promoted the task but did not start its worker"
    _wait_done(queued.task_id)


def test_expiry_patrol_starts_mining_queued_behind_a_crashed_writer(writer_env):
    factory, _mirror, _entered, _release = writer_env
    with factory() as db:
        TL.acquire_write_slot(db, task_id="crashed-writer", run_id="crashed-writer")
        # Seed an hour-old heartbeat to represent a process that has gone away.
        db.get(TaskLock, TL.LOCK_DUCKDB_WRITE).heartbeat_at = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        )
        db.commit()
    mined = threading.Event()

    def stages(_ctx):
        mined.set()
        return {"message_zh": "expiry resumed queued mining"}

    queued = TR.submit_mining_run(
        {"run_id": "queued-after-expiry"}, run_id="queued-after-expiry",
        stage_runner=stages,
    )
    assert queued.queue_position == 1
    TL.patrol_once(session_factory=factory)

    assert mined.wait(3), "expired write owner was replaced without waking its waiter"
    _wait_done(queued.task_id)


def test_running_mirror_reports_heartbeat_while_chunk_is_busy(writer_env):
    _factory, mirror, entered, _release = writer_env
    task = MT.create_mirror_task(
        preset="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        mirror_func=mirror, skip_disk_check=True,
    )
    assert entered.wait(5)
    heartbeat = AT.get_async_task_heartbeat(task["task_id"])
    assert heartbeat["status"] == "running"
    assert heartbeat["heartbeat_at"] is not None, "busy mirror has no worker heartbeat"


def test_busy_mirror_keeps_task_and_write_slot_heartbeats_fresh(writer_env, monkeypatch):
    factory, mirror, entered, _release = writer_env
    monkeypatch.setattr(MT, "TASK_HEARTBEAT_SECONDS", 0.05)
    task = MT.create_mirror_task(
        preset="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        mirror_func=mirror, skip_disk_check=True,
    )
    assert entered.wait(5)
    first_task = AT.get_async_task_heartbeat(task["task_id"])["heartbeat_at"]
    with factory() as db:
        first_lock = TL.get_lock_status(db).duckdb_write["heartbeatAt"]
    until = time.monotonic() + 3
    while time.monotonic() < until:
        heartbeat = AT.get_async_task_heartbeat(task["task_id"])
        with factory() as db:
            lock = TL.get_lock_status(db).duckdb_write
        if heartbeat["heartbeat_at"] > first_task and lock["heartbeatAt"] > first_lock:
            assert heartbeat["status"] == "running"
            assert lock["taskId"] == task["task_id"]
            return
        time.sleep(0.025)
    raise AssertionError("busy mirror did not refresh both heartbeats")


@pytest.mark.parametrize("exit_status", ["failed", "cancelled"])
def test_unsuccessful_mirror_exit_also_starts_waiting_mining(writer_env, exit_status):
    _factory, mirror, entered, release = writer_env

    def exiting_mirror(db, **kwargs):
        result = mirror(db, **kwargs)
        if exit_status == "failed":
            raise RuntimeError("isolated mirror failure")
        kwargs["should_cancel"]()
        return result

    owner = MT.create_mirror_task(
        preset="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        mirror_func=exiting_mirror, skip_disk_check=True,
    )
    assert entered.wait(5)
    mined = threading.Event()

    def stages(_ctx):
        mined.set()
        return {"message_zh": "resumed after unsuccessful mirror"}

    queued = TR.submit_mining_run(
        {"run_id": f"queued-after-{exit_status}"}, run_id=f"queued-after-{exit_status}",
        stage_runner=stages,
    )
    assert queued.queue_position == 1
    if exit_status == "cancelled":
        AT.cancel_async_task(owner["task_id"])
    release.set()

    assert mined.wait(3)
    _wait_done(queued.task_id)
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if AT.get_async_task_heartbeat(owner["task_id"])["status"] == exit_status:
            return
        time.sleep(0.025)
    raise AssertionError("mirror did not retain its real terminal result")


def test_cancelled_waiter_does_not_run_when_mirror_releases(writer_env):
    factory, mirror, entered, release = writer_env
    owner = MT.create_mirror_task(
        preset="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        mirror_func=mirror, skip_disk_check=True,
    )
    assert entered.wait(5)
    mined = threading.Event()

    def stages(_ctx):
        mined.set()
        return {}

    queued = TR.submit_mining_run(
        {"run_id": "cancelled-waiter"}, run_id="cancelled-waiter", stage_runner=stages,
    )
    TR.request_mining_cancellation(queued.task_id)
    assert AT.get_async_task_heartbeat(queued.task_id)["status"] == "cancelled"
    with factory() as db:
        lock = TL.get_lock_status(db).duckdb_write
    assert lock["taskId"] == owner["task_id"]
    assert lock["queue"] == []
    release.set()
    _wait_done(owner["task_id"])
    until = time.monotonic() + 3
    while time.monotonic() < until:
        with factory() as db:
            locks = TL.get_lock_status(db)
        if not locks.duckdb_write["busy"]:
            assert not locks.mining_domain["busy"]
            assert not mined.is_set()
            return
        time.sleep(0.025)
    raise AssertionError("cancelled waiter left a shared lock behind")


def test_stalled_live_worker_keeps_both_locks_until_it_really_exits(writer_env):
    factory, _mirror, entered, release = writer_env
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
    with factory() as db:
        MS.create_run(
            db, run_id="stalled-live-worker", candidate_pool_snapshot_id="fixture-snapshot",
            data_cutoff_at=old, start_date=old, end_date=old,
            created_at=old, updated_at=old,
        )

    def blocked_stages(_ctx):
        entered.set()
        assert release.wait(10)
        return {"message_zh": "worker reached cooperative stop"}

    submitted = TR.submit_mining_run(
        {"run_id": "stalled-live-worker"}, run_id="stalled-live-worker",
        stage_runner=blocked_stages,
    )
    assert entered.wait(5)
    assert WD.watchdog_once(session_factory=factory) == ["stalled-live-worker"]
    with factory() as db:
        detail = MS.get_run_detail(db, "stalled-live-worker")
        locks = TL.get_lock_status(db)
    assert detail["status"] == "failed"
    assert detail["error_code"] == "STALLED"
    assert AT.get_async_task_heartbeat(submitted.task_id)["cancel_requested"] is True
    assert locks.mining_domain["busy"] is True
    assert locks.duckdb_write["busy"] is True

    # A second orphan sweep or heartbeat expiry must not bypass the live writer.
    WD.watchdog_once(session_factory=factory)
    TL.patrol_once(session_factory=factory, timeout_seconds=0)
    with factory() as db:
        locks = TL.get_lock_status(db)
    assert locks.mining_domain["taskId"] == submitted.task_id
    assert locks.duckdb_write["taskId"] == submitted.task_id
    with pytest.raises(TL.MiningDomainBusy):
        TR.submit_mining_run({"run_id": "too-early"}, run_id="too-early",
                             stage_runner=TR.skeleton_stage_runner)

    release.set()
    until = time.monotonic() + 5
    while time.monotonic() < until:
        with factory() as db:
            locks = TL.get_lock_status(db)
        if not locks.duckdb_write["busy"] and not locks.mining_domain["busy"]:
            break
        time.sleep(0.025)
    else:
        raise AssertionError("exited stalled worker did not release its locks")
    with factory() as db:
        detail = MS.get_run_detail(db, "stalled-live-worker")
    assert detail["status"] == "failed"
    assert detail["error_code"] == "STALLED"
    assert AT.get_async_task_heartbeat(submitted.task_id)["status"] == "cancelled"
    next_task = TR.submit_mining_run(
        {"run_id": "after-stalled-exit"}, run_id="after-stalled-exit",
        stage_runner=TR.skeleton_stage_runner,
    )
    _wait_done(next_task.task_id)


def test_terminal_mirror_with_live_writer_delays_queue_promotion_until_exit(writer_env):
    factory, mirror, entered, release = writer_env
    owner = MT.create_mirror_task(
        preset="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        mirror_func=mirror, skip_disk_check=True,
    )
    assert entered.wait(5)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory() as db:
        MS.create_run(
            db, run_id="wait-for-live-terminal-mirror", candidate_pool_snapshot_id="fixture-snapshot",
            data_cutoff_at=now, start_date=now, end_date=now,
        )
    mined = threading.Event()

    def stages(ctx):
        assert ctx.holds_duckdb_write
        mined.set()
        return {"message_zh": "started only after old writer exit"}

    queued = TR.submit_mining_run(
        {"run_id": "wait-for-live-terminal-mirror"},
        run_id="wait-for-live-terminal-mirror", stage_runner=stages,
    )
    TSM.transition(owner["task_id"], "failed")
    with factory() as db:
        # Model a delayed heartbeat while the original chunk is still in flight.
        db.get(TaskLock, TL.LOCK_DUCKDB_WRITE).heartbeat_at = now - timedelta(hours=1)
        db.commit()
    WD.watchdog_once(session_factory=factory)
    TL.patrol_once(session_factory=factory)
    with factory() as db:
        locks = TL.get_lock_status(db)
    assert locks.duckdb_write["taskId"] == owner["task_id"]
    assert locks.duckdb_write["queue"] == [queued.task_id]
    assert AT.get_async_task_heartbeat(queued.task_id)["status"] == "queued"
    assert not mined.is_set()

    release.set()
    assert mined.wait(3), "waiting mining was not promoted after the old writer exited"
    _wait_done(queued.task_id)
    assert AT.get_async_task_heartbeat(owner["task_id"])["status"] == "failed"


def test_concurrent_recovery_starts_a_promoted_worker_only_once(writer_env):
    factory, _mirror, entered, release = writer_env
    task = AT.create_async_task(TR.TASK_TYPE, {"run_id": "concurrent-recovery"},
                                 use_control_plane=True)
    with factory() as db:
        TL.acquire_mining_lock(db, task_id=task.id, run_id="concurrent-recovery")
        TL.acquire_write_slot(db, task_id=task.id, run_id="concurrent-recovery")
    write_started = threading.Event()
    allow_write = threading.Event()
    calls = []

    def stages(_ctx):
        calls.append("started")
        entered.set()
        assert release.wait(10)
        return {}

    # Delay the real DB adapter's UPDATE to model a slow queued->running commit.
    # The first real thread exists already even though a second reader sees queued.
    def slow_write(_conn, _cursor, statement, _params, _context, _many):
        if statement.upper().startswith("UPDATE ASYNC_TASKS"):
            write_started.set()
            assert allow_write.wait(10)

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", slow_write)
    try:
        assert TR.wake_promoted_mining_task(task.id, stage_runner=stages)
        assert write_started.wait(5)
        assert AT.get_async_task_heartbeat(task.id)["status"] == "queued"
        assert not TR.wake_promoted_mining_task(task.id, stage_runner=stages)
        allow_write.set()
        assert entered.wait(5)
        assert calls == ["started"]
        release.set()
        _wait_done(task.id)
    finally:
        allow_write.set()
        release.set()
        AT.wait_workers_stopped(timeout_seconds=5)
        event.remove(engine, "before_cursor_execute", slow_write)


def test_resume_mirror_rejects_busy_writer_without_creating_or_queueing_a_task(writer_env):
    factory, mirror, entered, _release = writer_env
    old = AT.create_async_task(MT.TASK_TYPE, {
        "preset": "custom", "start_date": "2025-01-01", "end_date": "2025-01-02",
        "chunks": [["2025-01-01", "2025-01-02"]], "batch_size": 1000,
    }, use_control_plane=True)
    TSM.transition(old.id, "running")
    TSM.transition(old.id, "failed")
    active = MT.create_mirror_task(
        preset="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        mirror_func=mirror, skip_disk_check=True,
    )
    assert entered.wait(5)
    with factory() as db:
        before = MT.list_mirror_tasks(db)
    resumed = threading.Event()

    def forbidden_mirror(_db, **_kwargs):
        resumed.set()
        return type("Result", (), {"to_dict": lambda self: {"rows_written": 1}})()

    with pytest.raises(RuntimeError, match="DUCKDB_WRITE_BUSY"):
        MT.resume_mirror_task(task_id=old.id, mirror_func=forbidden_mirror)
    with factory() as db:
        after = MT.list_mirror_tasks(db)
        locks = TL.get_lock_status(db)
    assert len(after) == len(before)
    assert locks.duckdb_write["taskId"] == active["task_id"]
    assert locks.duckdb_write["queue"] == []
    assert not resumed.is_set()


def test_competing_mirror_creations_start_only_the_atomic_write_slot_winner(writer_env):
    factory, mirror, entered, _release = writer_env
    barrier = threading.Barrier(2)
    seen = threading.local()
    calls = []

    def synchronize_empty_peek(_conn, _cursor, statement, _params, _context, _many):
        if (threading.current_thread().name.startswith("mirror-submit")
                and statement.upper().startswith("SELECT TASK_LOCKS.")
                and not getattr(seen, "peek_done", False)):
            seen.peek_done = True
            barrier.wait(5)  # both real SELECTs observed the original empty table

    def gated_mirror(db, **kwargs):
        calls.append("writer started")
        return mirror(db, **kwargs)

    def submit(day):
        try:
            result = MT.create_mirror_task(
                preset="custom", start_date=date(2026, 1, day), end_date=date(2026, 1, day + 1),
                mirror_func=gated_mirror, skip_disk_check=True,
            )
            return "created", result
        except RuntimeError as exc:
            assert "DUCKDB_WRITE_BUSY" in str(exc)
            return "blocked", str(exc)

    engine = factory.kw["bind"]
    event.listen(engine, "after_cursor_execute", synchronize_empty_peek)
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="mirror-submit") as pool:
            pending = [pool.submit(submit, day) for day in (1, 3)]
            results = [future.result(timeout=8) for future in pending]
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_empty_peek)
    assert [kind for kind, _ in results].count("created") == 1
    assert [kind for kind, _ in results].count("blocked") == 1
    assert entered.wait(5)
    assert calls == ["writer started"]
    winner = next(result for kind, result in results if kind == "created")
    with factory() as db:
        lock = TL.get_lock_status(db).duckdb_write
        tasks = MT.list_mirror_tasks(db)
    assert lock["taskId"] == winner["task_id"]
    assert lock["queue"] == []
    assert sorted(task["status"] for task in tasks) == ["failed", "running"]
