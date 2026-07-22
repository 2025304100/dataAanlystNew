"""白盒测试 - WP-S.5 任务防卡死状态机 (app.services.task_state_machine)。

覆盖：
1. 状态转换合法性（白名单 + 终态保护）
2. 巡检 interrupted 检测
3. 巡检 stalled 检测
4. 巡检不误判健康任务
5. 取消释放资源（executor/duckdb_lock/session/file_lock）
6. 恢复 resume_task（启动新 worker + 传 batch_recovery）
7. 心跳与进度更新（last_progress_at 在 percent 增加时才更新；大跳被截断）
8. enter_stage 重置阶段开始/进度基线
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.async_task import AsyncTaskRecord
from app.services import async_tasks, task_state_machine as tsm

pytestmark = pytest.mark.whitebox


# ---------- 辅助 ----------

def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_task(
    db_session,
    *,
    task_id: str | None = None,
    status: str = "queued",
    stage: str = "prepare",
    percent: float = 0.0,
    task_type: str = "market_data_sync",
    **extra,
) -> AsyncTaskRecord:
    """创建任务记录，便于测试直接控制字段。"""
    import uuid
    task = AsyncTaskRecord(
        id=task_id or f"qa-task-{uuid.uuid4().hex[:8]}",
        task_type=task_type,
        status=status,
        stage=stage,
        percent=percent,
        message="",
        total=10,
    )
    for key, value in extra.items():
        setattr(task, key, value)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ============================================================================
# 1. 状态转换合法性
# ============================================================================

def test_can_transition_queued_to_running():
    assert tsm.can_transition("queued", "running") is True


def test_can_transition_running_to_interrupted():
    assert tsm.can_transition("running", "interrupted") is True


def test_can_transition_running_to_stalled():
    assert tsm.can_transition("running", "stalled") is True


def test_can_transition_done_to_running_illegal():
    """done 是终态，禁止转回 running。"""
    assert tsm.can_transition("done", "running") is False


def test_can_transition_cancelled_to_running_illegal():
    """cancelled 是终态，禁止转出。"""
    assert tsm.can_transition("cancelled", "running") is False


def test_transition_done_to_running_raises(db_session):
    """transition() 应拒绝非法转换（依赖 _set_task 终态保护）。"""
    task = _create_task(db_session, status="done", stage="done", percent=100.0)
    with pytest.raises(ValueError, match="Illegal task state transition"):
        tsm.transition(task.id, "running", db=db_session)


def test_transition_queued_to_running_succeeds(db_session):
    """queued → running 合法，应自动设置 started_at。"""
    task = _create_task(db_session, status="queued", stage="queued")
    read = tsm.transition(task.id, "running", db=db_session, stage="prepare")
    assert read.status == "running"
    db_session.refresh(task)
    assert task.started_at is not None


def test_transition_running_to_done_terminates(db_session):
    """running → done 应设置 finished_at。"""
    task = _create_task(db_session, status="running", stage="sync", percent=50.0)
    read = tsm.transition(task.id, "done", db=db_session, stage="done", percent=100.0)
    assert read.status == "done"
    db_session.refresh(task)
    assert task.finished_at is not None


# ============================================================================
# 2. 巡检 interrupted 检测
# ============================================================================

def test_patrol_marks_interrupted_when_worker_dead(db_session):
    """heartbeat 超时 + worker 线程已退出 → interrupted。"""
    # 构造一个 dead worker_thread_id（不存在于 threading._active）
    dead_thread_id = "deadbeef"  # 16 进制，几乎不可能命中真实线程
    task = _create_task(
        db_session,
        status="running",
        stage="sync",
        percent=30.0,
        heartbeat_at=_now_naive() - timedelta(minutes=10),
        worker_thread_id=dead_thread_id,
    )
    result = tsm.patrol_interrupted_and_stalled(db_session)
    db_session.refresh(task)

    assert task.id in result["interrupted"]
    assert task.status == "interrupted"
    assert task.suggested_action is not None
    assert "Worker" in task.suggested_action or "worker" in task.suggested_action


def test_patrol_skips_terminal_tasks(db_session):
    """终态任务（done/failed/cancelled）不应被巡检改动。"""
    done_task = _create_task(
        db_session,
        status="done",
        stage="done",
        percent=100.0,
        heartbeat_at=_now_naive() - timedelta(minutes=60),
        worker_thread_id="deadbeef",
    )
    failed_task = _create_task(
        db_session,
        status="failed",
        stage="failed",
        percent=10.0,
        heartbeat_at=_now_naive() - timedelta(minutes=60),
        worker_thread_id="deadbeef",
    )
    cancelled_task = _create_task(
        db_session,
        status="cancelled",
        stage="cancelled",
        percent=20.0,
        heartbeat_at=_now_naive() - timedelta(minutes=60),
        worker_thread_id="deadbeef",
    )

    result = tsm.patrol_interrupted_and_stalled(db_session)
    assert result["interrupted"] == []
    assert result["stalled"] == []

    db_session.refresh(done_task)
    db_session.refresh(failed_task)
    db_session.refresh(cancelled_task)
    assert done_task.status == "done"
    assert failed_task.status == "failed"
    assert cancelled_task.status == "cancelled"


# ============================================================================
# 3. 巡检 stalled 检测
# ============================================================================

def test_patrol_marks_stalled_when_stage_budget_exceeded(db_session):
    """阶段预算超时 + 无进度 → stalled。"""
    now = _now_naive()
    task = _create_task(
        db_session,
        status="running",
        stage="sync",
        percent=50.0,
        heartbeat_at=now,                      # 心跳新鲜，不会被判 interrupted
        stage_budget_seconds=60,
        stage_started_at=now - timedelta(minutes=5),
        last_progress_at=now - timedelta(minutes=3),
        last_progress_percent=50.0,
        worker_thread_id=None,
    )
    result = tsm.patrol_interrupted_and_stalled(db_session)
    db_session.refresh(task)

    assert task.id in result["stalled"]
    assert task.status == "stalled"
    assert task.suggested_action is not None
    assert "阶段" in task.suggested_action or "卡住" in task.suggested_action


# ============================================================================
# 4. 巡检不误判健康任务
# ============================================================================

def test_patrol_skips_healthy_running_task(db_session):
    """心跳新鲜的 running 任务应保持 running。"""
    task = _create_task(
        db_session,
        status="running",
        stage="sync",
        percent=10.0,
        heartbeat_at=_now_naive() - timedelta(seconds=30),
        worker_thread_id=hex(threading.get_ident()),
    )
    result = tsm.patrol_interrupted_and_stalled(db_session)
    db_session.refresh(task)

    assert result["interrupted"] == []
    assert result["stalled"] == []
    assert task.status == "running"
    assert task.last_patrol_at is not None


# ============================================================================
# 5. 取消释放资源
# ============================================================================

class _MockExecutor:
    """模拟 ThreadPoolExecutor，记录 shutdown 调用。"""

    def __init__(self):
        self.shutdown_called = False
        self.shutdown_wait = None

    def shutdown(self, wait=True):
        self.shutdown_called = True
        self.shutdown_wait = wait


class _MockLock:
    """模拟可释放的锁。"""

    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class _MockSession:
    """模拟 SQLAlchemy Session。"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_cancel_task_with_cleanup_releases_resources(db_session):
    """取消任务应释放 executor / duckdb_lock / session。"""
    task = _create_task(db_session, status="running", stage="sync", percent=20.0)

    executor = _MockExecutor()
    duckdb_lock = _MockLock()
    session = _MockSession()
    tsm.register_task_resource(
        task.id,
        executor=executor,
        duckdb_lock=duckdb_lock,
        session=session,
    )

    # 不 patch get_session_local：让应用代码用真实的 session factory（绑定到测试 SQLite）。
    # 这样每个内部 session 都会正常 commit + close，不会污染测试 db_session。
    read = tsm.cancel_task_with_cleanup(task.id)

    # 让测试 session 丢弃缓存，从 DB 重读最新状态
    db_session.expire_all()
    fresh = db_session.get(AsyncTaskRecord, task.id)
    assert read.status == "cancelled"
    # cancel_requested 在 DB 中以 INTEGER 存储，重读后为 1/0 而非 bool
    assert bool(fresh.cancel_requested) is True
    assert executor.shutdown_called is True
    assert duckdb_lock.released is True
    assert session.closed is True
    # 资源句柄已清理
    assert tsm.get_task_resource(task.id) is None


def test_cancel_task_with_cleanup_terminal_state_keeps_cancelled(db_session):
    """对已 cancelled 的任务再次取消，应保持 cancelled，不抛异常。"""
    task = _create_task(db_session, status="cancelled", stage="cancelled", percent=0.0)
    read = tsm.cancel_task_with_cleanup(task.id)
    assert read.status == "cancelled"


# ============================================================================
# 6. 恢复 resume_task
# ============================================================================

def test_resume_task_starts_new_worker_and_passes_batch_recovery(db_session):
    """从 interrupted 恢复应启动新 worker 并传入 batch_recovery。"""
    import json
    batch_recovery = [
        {"batch_id": "batch-1", "scope": "all", "processed_count": 50, "committed_at": "2026-07-19T10:00:00"},
    ]
    task = _create_task(
        db_session,
        status="interrupted",
        stage="sync",
        percent=50.0,
        batch_recovery_json=json.dumps(batch_recovery),
    )

    captured = {}

    def worker_func(task_id, batch_recovery=None):
        captured["task_id"] = task_id
        captured["batch_recovery"] = batch_recovery

    started_calls = []

    def fake_start_worker(task_id, worker):
        # 捕获 worker 并同步执行（避免线程时序问题）
        started_calls.append((task_id, worker))
        worker(task_id)

    # patch _start_worker 让 worker 同步执行；不 patch get_session_local，
    # 让 tsm 用真实 session factory 操作同一 SQLite 文件
    with patch.object(async_tasks, "_start_worker", side_effect=fake_start_worker):
        read = tsm.resume_task(task.id, worker_func)

    assert read.status == "running"
    # 从测试 session 重新读取最新状态
    db_session.expire_all()
    fresh = db_session.get(AsyncTaskRecord, task.id)
    assert fresh.heartbeat_at is not None
    assert fresh.cancel_requested in (None, False)
    # worker 被调用且收到 batch_recovery
    assert captured["task_id"] == task.id
    assert captured["batch_recovery"] == batch_recovery
    assert len(started_calls) == 1


def test_resume_task_rejects_non_resumable_state(db_session):
    """对 done/failed/cancelled 状态调用 resume 应抛 ValueError。"""
    task = _create_task(db_session, status="done", stage="done", percent=100.0)
    with pytest.raises(ValueError, match="not resumable"):
        tsm.resume_task(task.id, lambda tid, batch_recovery=None: None)


# ============================================================================
# 7. 心跳与进度更新
# ============================================================================

def test_update_heartbeat_sets_heartbeat_at(db_session):
    """update_heartbeat 应更新 heartbeat_at。"""
    task = _create_task(db_session, status="running", stage="sync", percent=10.0)
    before = _now_naive()
    time.sleep(0.01)
    tsm.update_heartbeat(task.id, current_step_description="处理 symbol 000001", db=db_session)
    db_session.refresh(task)
    assert task.heartbeat_at is not None
    assert task.heartbeat_at > before
    assert task.current_step_description == "处理 symbol 000001"


def test_update_progress_advances_last_progress_at_on_increase(db_session):
    """percent 真正增加时，last_progress_at 应被更新。"""
    task = _create_task(
        db_session,
        status="running",
        stage="sync",
        percent=20.0,
        last_progress_percent=20.0,
        last_progress_at=_now_naive() - timedelta(minutes=5),
    )
    old_last_progress = task.last_progress_at
    tsm.update_progress(task.id, percent=25.0, db=db_session)
    db_session.refresh(task)
    assert task.percent == 25.0
    assert task.last_progress_percent == 25.0
    assert task.last_progress_at > old_last_progress


def test_update_progress_does_not_advance_last_progress_when_unchanged(db_session):
    """percent 不变时，last_progress_at 不应更新（防止误判健康）。"""
    old_progress_at = _now_naive() - timedelta(minutes=5)
    task = _create_task(
        db_session,
        status="running",
        stage="sync",
        percent=30.0,
        last_progress_percent=30.0,
        last_progress_at=old_progress_at,
    )
    tsm.update_progress(task.id, percent=30.0, db=db_session)
    db_session.refresh(task)
    assert task.last_progress_at == old_progress_at
    assert task.last_progress_percent == 30.0


def test_update_progress_truncates_large_jump(db_session):
    """75% → 100% 跨度过大（>30%），应被截断为 75 + 30 = 105 → 100（钳到 100）。"""
    task = _create_task(
        db_session,
        status="running",
        stage="sync",
        percent=75.0,
        last_progress_percent=75.0,
        last_progress_at=_now_naive(),
    )
    tsm.update_progress(task.id, percent=100.0, db=db_session)
    db_session.refresh(task)
    # 75 + 30 = 105 → 但目标 100，clamp 到 100
    assert task.percent == 100.0
    # last_progress_percent 应反映截断后的值
    assert task.last_progress_percent == 100.0


def test_update_progress_truncates_more_than_thirty_percent_jump(db_session):
    """50% → 95% 跨度 45%，应被截断为 50 + 30 = 80。"""
    task = _create_task(
        db_session,
        status="running",
        stage="sync",
        percent=50.0,
        last_progress_percent=50.0,
        last_progress_at=_now_naive(),
    )
    tsm.update_progress(task.id, percent=95.0, db=db_session)
    db_session.refresh(task)
    assert task.percent == 80.0  # 50 + 30


def test_update_progress_skipped_for_terminal_task(db_session):
    """终态任务的进度更新应被忽略（不抛异常，也不改 percent）。"""
    task = _create_task(db_session, status="done", stage="done", percent=100.0)
    tsm.update_progress(task.id, percent=50.0, db=db_session)
    db_session.refresh(task)
    assert task.percent == 100.0


def test_update_progress_with_batch_recovery_serializes(db_session):
    """batch_recovery 应被序列化为 JSON。"""
    task = _create_task(db_session, status="running", stage="sync", percent=10.0)
    batches = [{"batch_id": "b1", "processed_count": 10}]
    tsm.update_progress(task.id, batch_recovery=batches, db=db_session)
    db_session.refresh(task)
    import json
    assert task.batch_recovery_json is not None
    loaded = json.loads(task.batch_recovery_json)
    assert loaded == batches


# ============================================================================
# 8. enter_stage
# ============================================================================

def test_enter_stage_resets_stage_fields(db_session):
    """enter_stage 应重置 stage / stage_started_at / last_progress_at / stage_budget_seconds。"""
    task = _create_task(
        db_session,
        status="running",
        stage="prepare",
        percent=10.0,
        stage_started_at=_now_naive() - timedelta(minutes=10),
        last_progress_at=_now_naive() - timedelta(minutes=10),
        stage_budget_seconds=30,
    )
    old_stage_started = task.stage_started_at
    time.sleep(0.01)
    tsm.enter_stage(task.id, "sync", stage_budget_seconds=120, db=db_session)
    db_session.refresh(task)
    assert task.stage == "sync"
    assert task.stage_started_at > old_stage_started
    assert task.last_progress_at > old_stage_started
    assert task.stage_budget_seconds == 120


# ============================================================================
# 集成：通过 async_tasks.get_async_task 触发巡检
# ============================================================================

def test_get_async_task_invokes_patrol(db_session):
    """get_async_task 内部应触发巡检，把 dead worker 的任务标记为 interrupted。"""
    task = _create_task(
        db_session,
        status="running",
        stage="sync",
        percent=30.0,
        heartbeat_at=_now_naive() - timedelta(minutes=10),
        worker_thread_id="deadbeef",
    )
    # 不 patch：让 async_tasks.get_async_task 用真实 session factory（同一 SQLite）
    read = async_tasks.get_async_task(task.id)
    assert read is not None
    assert read.status == "interrupted"
