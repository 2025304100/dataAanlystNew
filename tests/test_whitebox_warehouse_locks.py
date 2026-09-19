"""WPD-03 白盒测试 — DuckDB 仓库锁治理和僵尸任务恢复。

覆盖场景：
 1. 基本锁获取/释放
 2. 重复获取返回 held_by_other
 3. stale lock 自动回收
 4. 释放他人锁被拒绝
 5. 僵尸任务诊断（heartbeat 超期）
 6. 僵尸任务恢复（标记 failed）
 7. 正常任务不被误判
 8. stage_budget 超期检测
 9. safe_write_context 失败回滚
10. safe_write_context 成功提交
11. 诊断脚本可运行

测试不依赖真实 DuckDB 文件（使用 tmp_path）和真实进程（PID=999999）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.async_task import AsyncTaskRecord
from app.models.factor_runtime import FactorSystemConfig
from app.services.factors.warehouse_locks import (
    StaleTaskDiagnostic,
    WarehouseLockAcquisition,
    WarehouseLockInfo,
    WarehouseLockUnavailable,
    acquire_warehouse_lock,
    cleanup_stale_warehouse_lock,
    diagnose_stale_tasks,
    diagnose_warehouse_lock,
    recover_stale_tasks,
    release_warehouse_lock,
    run_warehouse_lock_diagnostics,
)

pytestmark = pytest.mark.whitebox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _write_fake_lockfile(
    warehouse_path: Path, *, pid: int, acquired_at: datetime | None = None
) -> Path:
    """Manually create a lockfile with a specific PID (for stale-lock tests)."""
    lockfile = Path(str(warehouse_path) + ".lock")
    content = json.dumps(
        {
            "pid": pid,
            "acquired_at": (acquired_at or _utcnow_naive()).isoformat(),
        }
    )
    lockfile.write_text(content, encoding="utf-8")
    return lockfile


def _create_task(
    db_session,
    *,
    task_id: str,
    status: str = "running",
    task_type: str = "factor_pipeline",
    heartbeat_at: datetime | None = None,
    updated_at: datetime | None = None,
    stage_started_at: datetime | None = None,
    stage_budget_seconds: int | None = None,
) -> AsyncTaskRecord:
    task = AsyncTaskRecord(
        id=task_id,
        task_type=task_type,
        status=status,
        stage=status,
        percent=0.0,
    )
    if heartbeat_at is not None:
        task.heartbeat_at = heartbeat_at
    if updated_at is not None:
        task.updated_at = updated_at
    if stage_started_at is not None:
        task.stage_started_at = stage_started_at
    if stage_budget_seconds is not None:
        task.stage_budget_seconds = stage_budget_seconds
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _setup_config(db_session, warehouse_path: str) -> None:
    db_session.add(
        FactorSystemConfig(
            id=1,
            feature_enabled=1,
            warehouse_path=str(warehouse_path),
            updated_by="test",
        )
    )
    db_session.commit()


# ---------------------------------------------------------------------------
# 1. 基本锁获取/释放
# ---------------------------------------------------------------------------


def test_basic_acquire_and_release(tmp_path):
    """acquire → release，锁文件正确创建和删除。"""
    wh = tmp_path / "basic.duckdb"
    lockfile = Path(str(wh) + ".lock")

    acquisition = acquire_warehouse_lock(wh, timeout_seconds=0)
    assert acquisition.acquired is True
    assert acquisition.reason is None
    assert lockfile.exists()

    released = release_warehouse_lock(wh)
    assert released is True
    assert not lockfile.exists()


# ---------------------------------------------------------------------------
# 2. 重复获取返回 held_by_other
# ---------------------------------------------------------------------------


def test_duplicate_acquire_returns_held_by_other(tmp_path):
    """A 持有锁时 B acquire 返回 acquired=False, reason='held_by_other'。"""
    wh = tmp_path / "dup.duckdb"

    # A acquires (using current PID)
    a = acquire_warehouse_lock(wh, owner_pid=os.getpid())
    assert a.acquired is True

    # B tries with a *different* PID — should fail
    b = acquire_warehouse_lock(wh, owner_pid=os.getpid() + 1)
    assert b.acquired is False
    assert b.reason == "held_by_other"
    assert b.owner_pid == os.getpid()  # diagnostics report the real owner

    release_warehouse_lock(wh, owner_pid=os.getpid())


# ---------------------------------------------------------------------------
# 3. stale lock 自动回收
# ---------------------------------------------------------------------------


def test_stale_lock_auto_recycled(tmp_path):
    """PID=999999（不存在）的锁文件，acquire 应自动清理并获取。"""
    wh = tmp_path / "stale.duckdb"
    lockfile = _write_fake_lockfile(wh, pid=999999)

    assert lockfile.exists()

    acquisition = acquire_warehouse_lock(wh, owner_pid=os.getpid())
    assert acquisition.acquired is True
    # Lockfile should have been recycled and recreated with our PID
    assert lockfile.exists()
    data = json.loads(lockfile.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()

    release_warehouse_lock(wh)


def test_cleanup_stale_lock_removes_dead_pid_lockfile(tmp_path):
    """cleanup_stale_warehouse_lock 对死 PID 锁文件返回 True 并删除。"""
    wh = tmp_path / "cleanup.duckdb"
    _write_fake_lockfile(wh, pid=999999)

    assert cleanup_stale_warehouse_lock(wh) is True
    assert not Path(str(wh) + ".lock").exists()


def test_cleanup_stale_lock_preserves_live_pid_lockfile(tmp_path):
    """cleanup_stale_warehouse_lock 对活 PID 锁文件返回 False 不删除。"""
    wh = tmp_path / "live.duckdb"
    _write_fake_lockfile(wh, pid=os.getpid())

    assert cleanup_stale_warehouse_lock(wh) is False
    assert Path(str(wh) + ".lock").exists()


# ---------------------------------------------------------------------------
# 4. 释放他人锁被拒绝
# ---------------------------------------------------------------------------


def test_release_other_process_lock_rejected(tmp_path):
    """A 持有锁，B 调用 release(owner_pid=B_pid) 应返回 False。"""
    wh = tmp_path / "reject.duckdb"

    # A acquires
    a_pid = os.getpid()
    acquire_warehouse_lock(wh, owner_pid=a_pid)

    # B (different PID) tries to release
    b_pid = a_pid + 1
    released = release_warehouse_lock(wh, owner_pid=b_pid)
    assert released is False
    assert Path(str(wh) + ".lock").exists()  # Lock still held by A

    # A can release
    assert release_warehouse_lock(wh, owner_pid=a_pid) is True


# ---------------------------------------------------------------------------
# 5. 僵尸任务诊断（heartbeat 超期）
# ---------------------------------------------------------------------------


def test_diagnose_stale_tasks_heartbeat_timeout(db_session):
    """heartbeat_at 早于 90s 前的 running 任务 → is_stale=True。"""
    old_time = _utcnow_naive() - timedelta(seconds=120)
    _create_task(
        db_session,
        task_id="stale-heartbeat",
        heartbeat_at=old_time,
        updated_at=old_time,
    )

    diagnostics = diagnose_stale_tasks(db_session)
    stale = [d for d in diagnostics if d.task_id == "stale-heartbeat"]
    assert len(stale) == 1
    assert stale[0].is_stale is True
    assert stale[0].stale_reason == "heartbeat_timeout"


# ---------------------------------------------------------------------------
# 6. 僵尸任务恢复
# ---------------------------------------------------------------------------


def test_recover_stale_tasks_marks_failed(db_session, tmp_path):
    """recover_stale_tasks 将僵尸任务标记为 failed，返回恢复记录。"""
    _setup_config(db_session, tmp_path / "recover.duckdb")
    old_time = _utcnow_naive() - timedelta(seconds=120)
    _create_task(
        db_session,
        task_id="recover-me",
        heartbeat_at=old_time,
        updated_at=old_time,
    )

    records = recover_stale_tasks(
        db_session, task_types=("factor_pipeline",)
    )
    assert len(records) == 1
    assert records[0]["task_id"] == "recover-me"
    assert records[0]["previous_status"] == "running"
    assert records[0]["new_status"] == "failed"
    assert records[0]["stale_reason"] == "heartbeat_timeout"

    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, "recover-me")
    assert task.status == "failed"
    assert task.stage == "failed"
    assert task.finished_at is not None


# ---------------------------------------------------------------------------
# 7. 正常任务不被误判
# ---------------------------------------------------------------------------


def test_fresh_task_not_stale(db_session):
    """heartbeat_at 在 30s 内的任务 → is_stale=False。"""
    recent = _utcnow_naive() - timedelta(seconds=30)
    _create_task(
        db_session,
        task_id="fresh-task",
        heartbeat_at=recent,
        updated_at=recent,
    )

    diagnostics = diagnose_stale_tasks(db_session)
    fresh = [d for d in diagnostics if d.task_id == "fresh-task"]
    assert len(fresh) == 1
    assert fresh[0].is_stale is False
    assert fresh[0].stale_reason is None


# ---------------------------------------------------------------------------
# 8. stage_budget 超期检测
# ---------------------------------------------------------------------------


def test_stage_budget_exceeded(db_session):
    """stage_started_at + stage_budget_seconds + grace < now → is_stale=True。"""
    now = _utcnow_naive()
    # stage started 600s ago, budget 60s, grace 300s → deadline = 360s ago
    _create_task(
        db_session,
        task_id="budget-exceeded",
        heartbeat_at=now,  # heartbeat is fresh
        updated_at=now,
        stage_started_at=now - timedelta(seconds=600),
        stage_budget_seconds=60,
    )

    diagnostics = diagnose_stale_tasks(
        db_session, stage_budget_grace_seconds=300
    )
    stale = [d for d in diagnostics if d.task_id == "budget-exceeded"]
    assert len(stale) == 1
    assert stale[0].is_stale is True
    assert stale[0].stale_reason == "stage_budget_exceeded"


def test_stage_budget_not_exceeded(db_session):
    """stage within budget → is_stale=False。"""
    now = _utcnow_naive()
    _create_task(
        db_session,
        task_id="budget-ok",
        heartbeat_at=now,
        updated_at=now,
        stage_started_at=now - timedelta(seconds=10),
        stage_budget_seconds=3600,
    )

    diagnostics = diagnose_stale_tasks(db_session)
    ok = [d for d in diagnostics if d.task_id == "budget-ok"]
    assert len(ok) == 1
    assert ok[0].is_stale is False


# ---------------------------------------------------------------------------
# 9. safe_write_context 失败回滚
# ---------------------------------------------------------------------------


def test_safe_write_context_rollback_on_failure(tmp_path):
    """INSERT 失败 → ROLLBACK 被执行且锁被释放，数据未写入。"""
    pytest.importorskip("duckdb")
    from app.services.factors.store import FactorWarehouse

    wh = FactorWarehouse(tmp_path / "rollback.duckdb")
    wh.initialize()

    # Insert valid data first to prove rollback doesn't corrupt it
    with wh.connection() as conn:
        conn.execute("CREATE TABLE t_rollback (id INTEGER PRIMARY KEY, v VARCHAR)")
        conn.execute("INSERT INTO t_rollback VALUES (1, 'keep')")

    lockfile = Path(str(wh.path) + ".lock")

    # Now attempt a write that fails inside safe_write_context
    with pytest.raises(Exception):
        with wh.safe_write_context(timeout_seconds=5) as conn:
            # Insert a duplicate PK → constraint violation
            conn.execute("INSERT INTO t_rollback VALUES (1, 'conflict')")

    # Lock must be released even after failure
    assert not lockfile.exists()

    # Original data intact (ROLLBACK worked)
    with wh.connection(read_only=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM t_rollback").fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# 10. safe_write_context 成功提交
# ---------------------------------------------------------------------------


def test_safe_write_context_commit_on_success(tmp_path):
    """正常 INSERT → COMMIT 且锁释放。"""
    pytest.importorskip("duckdb")
    from app.services.factors.store import FactorWarehouse

    wh = FactorWarehouse(tmp_path / "commit.duckdb")
    wh.initialize()

    lockfile = Path(str(wh.path) + ".lock")

    with wh.safe_write_context(timeout_seconds=5) as conn:
        conn.execute("CREATE TABLE t_commit (id INTEGER, label VARCHAR)")
        conn.execute("INSERT INTO t_commit VALUES (1, 'ok')")

    # Lock released after context exit
    assert not lockfile.exists()

    # Data committed
    with wh.connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT id, label FROM t_commit WHERE id = 1"
        ).fetchone()
        assert row == (1, "ok")


def test_safe_write_context_raises_when_locked(tmp_path):
    """锁被持有时 safe_write_context 抛出 WarehouseLockUnavailable。"""
    pytest.importorskip("duckdb")
    from app.services.factors.store import FactorWarehouse

    wh = FactorWarehouse(tmp_path / "locked.duckdb")
    wh.initialize()

    # Acquire the lock externally with current PID
    acquire_warehouse_lock(wh.path, owner_pid=os.getpid())

    try:
        # Attempt with a *different* PID and zero timeout → should raise
        # We simulate a different process by acquiring with our PID first,
        # then trying safe_write_context which also uses our PID.
        # Since safe_write_context uses os.getpid() and we already hold
        # the lock with the same PID, we need a different approach:
        # manually write a lockfile with a fake live PID.
        lockfile = Path(str(wh.path) + ".lock")
        lockfile.write_text(
            json.dumps({"pid": os.getpid(), "acquired_at": _utcnow_naive().isoformat()}),
            encoding="utf-8",
        )

        # Now try safe_write_context with timeout=0 — should fail
        # because the lock is held by our own PID (release_warehouse_lock
        # uses os.getpid(), but acquire checks the lockfile PID).
        # Actually, since the lockfile PID == os.getpid() and _pid_exists
        # returns True for our own PID, acquire will return held_by_other.
        with pytest.raises(WarehouseLockUnavailable):
            wh.safe_write_context(timeout_seconds=0)
    finally:
        release_warehouse_lock(wh.path)


# ---------------------------------------------------------------------------
# 11. 诊断脚本可运行
# ---------------------------------------------------------------------------


def test_run_warehouse_lock_diagnostics_returns_valid_dict(tmp_path):
    """run_warehouse_lock_diagnostics 返回合法 dict，包含所有必需字段。"""
    pytest.importorskip("duckdb")
    from app.services.factors.store import FactorWarehouse

    wh_path = tmp_path / "diag.duckdb"
    # Initialize so health() returns available=True
    wh = FactorWarehouse(wh_path)
    wh.initialize()

    report = run_warehouse_lock_diagnostics(wh_path)

    assert isinstance(report, dict)
    # Required top-level keys
    assert "warehouse_path" in report
    assert "file_size" in report
    assert "file_mtime" in report
    assert "lock_info" in report
    assert "related_processes" in report
    assert "duckdb_health" in report
    assert "diagnostic_timestamp" in report

    # lock_info structure
    li = report["lock_info"]
    assert "path" in li
    assert "is_locked" in li
    assert "lock_owner_pid" in li
    assert "diagnostic_method" in li
    assert "notes" in li

    # duckdb_health should report available since we initialized
    assert report["duckdb_health"]["available"] is True

    # File size should be non-zero (initialized DuckDB file)
    assert report["file_size"] is not None
    assert report["file_size"] > 0


def test_run_warehouse_lock_diagnostics_with_stale_lock(tmp_path):
    """诊断脚本能识别 stale lock 并在报告中标注。"""
    wh_path = tmp_path / "stale_diag.duckdb"
    _write_fake_lockfile(wh_path, pid=999999)

    report = run_warehouse_lock_diagnostics(wh_path)
    li = report["lock_info"]
    assert li["is_locked"] is False
    assert li["lock_owner_pid"] == 999999
    assert any("stale" in note.lower() for note in li["notes"])


# ---------------------------------------------------------------------------
# 补充：diagnose_warehouse_lock 返回类型验证
# ---------------------------------------------------------------------------


def test_diagnose_warehouse_lock_no_lockfile(tmp_path):
    """无锁文件时返回 is_locked=False。"""
    wh = tmp_path / "nolock.duckdb"
    info = diagnose_warehouse_lock(wh)
    assert isinstance(info, WarehouseLockInfo)
    assert info.is_locked is False
    assert info.lock_owner_pid is None


def test_diagnose_warehouse_lock_live_pid(tmp_path):
    """活 PID 锁文件 → is_locked=True。"""
    wh = tmp_path / "livelock.duckdb"
    _write_fake_lockfile(wh, pid=os.getpid())
    info = diagnose_warehouse_lock(wh)
    assert info.is_locked is True
    assert info.lock_owner_pid == os.getpid()
    assert info.lock_age_seconds is not None
    assert info.lock_age_seconds >= 0


# ---------------------------------------------------------------------------
# 补充：acquire with timeout 轮询
# ---------------------------------------------------------------------------


def test_acquire_with_timeout_waits_then_fails(tmp_path):
    """timeout > 0 但锁不释放 → 超时后返回 acquired=False。"""
    wh = tmp_path / "timeout.duckdb"
    # Hold lock with current PID
    acquire_warehouse_lock(wh, owner_pid=os.getpid())

    # Try to acquire with a different PID and short timeout
    result = acquire_warehouse_lock(
        wh, owner_pid=os.getpid() + 1, timeout_seconds=0.5
    )
    assert result.acquired is False
    assert result.reason == "held_by_other"

    release_warehouse_lock(wh)
