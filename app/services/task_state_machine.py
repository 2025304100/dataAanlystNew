"""WP-S.5 任务防卡死状态机。

提供异步任务状态转换白名单、心跳/进度更新、阶段预算管理、巡检
（interrupted/stalled 检测）、取消时资源释放、断点续传恢复能力。

设计要点：
- 不重写已稳定的 `app.services.async_tasks` 中的 `_set_task` / `_expire_stale_tasks`
  / `interrupt_orphaned_async_tasks` / `cancel_async_task` 逻辑，只在它们之上做
  增量扩展。
- 终态保护依赖 `_set_task`：done/failed/cancelled 不会被 worker 覆盖回 running。
- 巡检与资源释放全程 best-effort，异常仅记录日志，不抛出。
- 不输出敏感信息（数据库 URL、Token、密码等）到日志。
"""
from __future__ import annotations

import enum
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import AsyncTaskRead
from app.services import async_tasks

logger = logging.getLogger(__name__)

# ── 巡检阈值 ─────────────────────────────────────────────
# heartbeat_at 距 now 超过该阈值且 worker_thread_id 不再存活 → interrupted
HEARTBEAT_TIMEOUT = timedelta(minutes=5)
# 阶段预算超时 + 最后进度距 now 超过该阈值 → stalled
STALLED_NO_PROGRESS = timedelta(minutes=2)
# 进度更新单步最大百分比跳跃（防止 75→90 这种大跳，按 project_memory 硬约束 #5）
MAX_PROGRESS_STEP = 30.0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── 3.1 状态枚举 ─────────────────────────────────────────
class TaskState(str, enum.Enum):
    """异步任务全状态枚举（包含终态与可恢复中间态）。"""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    DONE = "done"
    FAILED = "failed"
    INTERRUPTED = "interrupted"   # worker 异常退出
    STALLED = "stalled"            # 超过阶段预算


# 终态集合：不可转出
_TERMINAL_STATES = frozenset({
    TaskState.DONE.value,
    TaskState.FAILED.value,
    TaskState.CANCELLED.value,
})

# ── 3.2 合法状态转换白名单 ───────────────────────────────
# 键：from_state，值：允许转到的目标状态集合
_TRANSITIONS: dict[str, frozenset[str]] = {
    TaskState.QUEUED.value: frozenset({
        TaskState.RUNNING.value,
        TaskState.CANCELLED.value,
    }),
    TaskState.RUNNING.value: frozenset({
        TaskState.DONE.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
        TaskState.INTERRUPTED.value,
        TaskState.STALLED.value,
        TaskState.PAUSED.value,
    }),
    TaskState.PAUSED.value: frozenset({
        TaskState.RUNNING.value,
        TaskState.CANCELLED.value,
    }),
    TaskState.INTERRUPTED.value: frozenset({
        TaskState.RUNNING.value,    # resume
        TaskState.CANCELLED.value,
    }),
    TaskState.STALLED.value: frozenset({
        TaskState.RUNNING.value,    # resume
        TaskState.CANCELLED.value,
        TaskState.FAILED.value,
    }),
}


def can_transition(from_state: str, to_state: str) -> bool:
    """判断状态转换是否合法。

    终态（done/failed/cancelled）不允许转出。
    """
    if from_state in _TERMINAL_STATES:
        return False
    allowed = _TRANSITIONS.get(from_state, frozenset())
    return to_state in allowed


def transition(
    task_id: str,
    to_state: str,
    *,
    db: Session | None = None,
    **fields: Any,
) -> AsyncTaskRead:
    """按白名单转换任务状态。

    内部调用 `async_tasks._set_task`，复用终态保护逻辑；
    若转换非法则抛出 ValueError。
    """
    own_session = db is None
    if db is None:
        SessionLocal = get_session_local()
        db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            raise ValueError(f"Async task not found: {task_id}")
        from_state = task.status
        if not can_transition(from_state, to_state):
            raise ValueError(
                f"Illegal task state transition: {from_state} -> {to_state} "
                f"(task_id={task_id})"
            )
        updates: dict[str, Any] = {"status": to_state}
        # 状态联动：进入终态时设置 finished_at
        if to_state in _TERMINAL_STATES and "finished_at" not in fields:
            updates["finished_at"] = _now()
        # 进入 running 时刷新 started_at（如果之前没有）
        if to_state == TaskState.RUNNING.value and task.started_at is None:
            updates["started_at"] = _now()
        updates.update(fields)
        record = async_tasks._set_task(db, task_id, **updates)
        return async_tasks._task_to_read(record)
    finally:
        if own_session:
            db.close()


# ── 3.3 心跳与进度更新 ───────────────────────────────────
def update_heartbeat(
    task_id: str,
    *,
    current_step_description: str | None = None,
    db: Session | None = None,
) -> None:
    """更新 worker 心跳时间。

    worker 在每个批次处理完成后调用，巡检依据该字段判断 worker 是否存活。
    """
    own_session = db is None
    if db is None:
        SessionLocal = get_session_local()
        db = SessionLocal()
    try:
        updates: dict[str, Any] = {"heartbeat_at": _now()}
        if current_step_description is not None:
            updates["current_step_description"] = current_step_description
        async_tasks._set_task(db, task_id, **updates)
    finally:
        if own_session:
            db.close()


def update_progress(
    task_id: str,
    *,
    processed: int | None = None,
    total: int | None = None,
    percent: float | None = None,
    current_step_description: str | None = None,
    batch_recovery: list[dict] | None = None,
    db: Session | None = None,
) -> None:
    """更新任务进度，自动维护 last_progress_at / last_progress_percent。

    - 仅当 `percent` 真正增加时才更新 last_progress_at，防止误判健康。
    - 单步跳跃不超过 MAX_PROGRESS_STEP（默认 30%），超过则截断并记录日志。
    """
    own_session = db is None
    if db is None:
        SessionLocal = get_session_local()
        db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            raise ValueError(f"Async task not found: {task_id}")
        # 终态任务忽略进度更新
        if task.status in _TERMINAL_STATES:
            return

        updates: dict[str, Any] = {}
        if processed is not None:
            updates["processed"] = int(processed)
        if total is not None:
            updates["total"] = int(total)
        if current_step_description is not None:
            updates["current_step_description"] = current_step_description
        if batch_recovery is not None:
            updates["batch_recovery_json"] = json.dumps(
                batch_recovery, ensure_ascii=False, default=str
            )

        new_percent: float | None = None
        if percent is not None:
            current_percent = float(task.percent or 0)
            last_percent = (
                float(task.last_progress_percent)
                if task.last_progress_percent is not None
                else current_percent
            )
            target_percent = float(percent)
            # 拒绝倒退（除非显式传入更小值，则按当前值兜底）
            if target_percent < current_percent:
                target_percent = current_percent
            # 限制单步跳跃
            step = target_percent - last_percent
            if step > MAX_PROGRESS_STEP:
                target_percent = last_percent + MAX_PROGRESS_STEP
                logger.warning(
                    "Task %s progress step too large: %s%% -> %s%%, truncated to %s%%",
                    task_id, last_percent, percent, target_percent,
                )
            new_percent = target_percent
            updates["percent"] = new_percent
            # 仅当 percent 真正增加时才更新 last_progress_at
            if new_percent > last_percent:
                updates["last_progress_at"] = _now()
                updates["last_progress_percent"] = new_percent

        if updates:
            async_tasks._set_task(db, task_id, **updates)
    finally:
        if own_session:
            db.close()


# ── 3.4 进入阶段 ─────────────────────────────────────────
def enter_stage(
    task_id: str,
    stage: str,
    *,
    stage_budget_seconds: int | None = None,
    db: Session | None = None,
) -> None:
    """进入新阶段，重置阶段开始时间与进度基线。"""
    own_session = db is None
    if db is None:
        SessionLocal = get_session_local()
        db = SessionLocal()
    try:
        now = _now()
        updates: dict[str, Any] = {
            "stage": stage,
            "stage_started_at": now,
            "last_progress_at": now,
        }
        if stage_budget_seconds is not None:
            updates["stage_budget_seconds"] = int(stage_budget_seconds)
        async_tasks._set_task(db, task_id, **updates)
    finally:
        if own_session:
            db.close()


# ── 3.5 巡检 patrol_interrupted_and_stalled ─────────────
def _is_worker_thread_alive(worker_thread_id: str | None) -> bool:
    """检查 worker 线程是否仍存活。

    利用 CPython 内部 `threading._active` 字典（线程 ID → Thread 对象）。
    若 worker_thread_id 不在该字典中，说明线程已退出。
    """
    if not worker_thread_id:
        # 没有记录 thread_id 视为不可判定，按"不存活"处理
        return False
    try:
        tid = int(worker_thread_id, 16)
    except (TypeError, ValueError):
        return False
    try:
        # threading._active 是 CPython 内部实现，跨版本兼容性需 best-effort
        active = threading._active  # type: ignore[attr-defined]
    except AttributeError:
        # 无法判定时保守按"存活"处理，避免误标 interrupted
        return True
    return tid in active


def patrol_interrupted_and_stalled(db: Session | None = None) -> dict[str, list[str]]:
    """巡检所有非终态任务，标记 interrupted / stalled。

    返回 `{"interrupted": [...], "stalled": [...], "recovered": [...]}`，
    全程 best-effort，异常仅记录日志。
    """
    result: dict[str, list[str]] = {
        "interrupted": [],
        "stalled": [],
        "recovered": [],
    }
    own_session = db is None
    if db is None:
        SessionLocal = get_session_local()
        db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(AsyncTaskRecord).where(
                    AsyncTaskRecord.status.in_(
                        (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value)
                    )
                )
            )
            .scalars()
            .all()
        )
        now = _now()
        for task in rows:
            try:
                _patrol_one(task, now, result)
            except Exception as exc:
                logger.warning(
                    "Patrol failed for task %s: %s", task.id, exc, exc_info=False
                )
        if rows:
            db.commit()
    except Exception as exc:
        logger.warning("Patrol interrupted_and_stalled failed: %s", exc, exc_info=False)
    finally:
        if own_session:
            db.close()
    return result


def _patrol_one(
    task: AsyncTaskRecord, now: datetime, result: dict[str, list[str]]
) -> None:
    """对单个任务执行巡检判定（不抛异常）。"""
    # 1) interrupted 判定：heartbeat 超时 + worker 线程不存活
    heartbeat = task.heartbeat_at
    if heartbeat is not None and (now - heartbeat) > HEARTBEAT_TIMEOUT:
        if not _is_worker_thread_alive(task.worker_thread_id):
            task.status = TaskState.INTERRUPTED.value
            task.stage = TaskState.INTERRUPTED.value
            task.suggested_action = "Worker 异常退出，可点击恢复继续处理"
            task.message = "Worker heartbeat timed out, marked as interrupted by patrol"
            task.last_patrol_at = now
            result["interrupted"].append(task.id)
            return
    # 2) stalled 判定：阶段预算超时 + 无进度
    if (
        task.stage_budget_seconds is not None
        and task.stage_started_at is not None
        and task.last_progress_at is not None
    ):
        stage_elapsed = (now - task.stage_started_at).total_seconds()
        no_progress_elapsed = now - task.last_progress_at
        if (
            stage_elapsed > float(task.stage_budget_seconds)
            and no_progress_elapsed > STALLED_NO_PROGRESS
        ):
            task.status = TaskState.STALLED.value
            task.stage = TaskState.STALLED.value
            task.suggested_action = "任务在某阶段卡住超过预算时间，可恢复继续或取消"
            task.message = "Stage budget exceeded with no progress, marked as stalled by patrol"
            task.last_patrol_at = now
            result["stalled"].append(task.id)
            return
    # 3) 健康任务：仅更新 last_patrol_at
    task.last_patrol_at = now


# ── 3.6 取消与资源释放 ───────────────────────────────────
@dataclass
class TaskResourceHandle:
    """任务持有的可释放资源句柄。"""

    executor: Any | None = None              # ThreadPoolExecutor
    duckdb_lock: Any | None = None           # 因子仓库单写锁（RLock）
    session: Any | None = None               # 额外数据库 Session
    file_locks: list[Any] = field(default_factory=list)


# task_id → 资源句柄；线程安全
_task_resources: dict[str, TaskResourceHandle] = {}
_task_resources_lock = threading.Lock()


def register_task_resource(
    task_id: str,
    *,
    executor: Any | None = None,
    duckdb_lock: Any | None = None,
    session: Any | None = None,
    file_lock: Any | None = None,
) -> None:
    """让 worker 在创建资源时注册，便于取消时统一释放。"""
    with _task_resources_lock:
        handle = _task_resources.get(task_id)
        if handle is None:
            handle = TaskResourceHandle()
            _task_resources[task_id] = handle
        if executor is not None:
            handle.executor = executor
        if duckdb_lock is not None:
            handle.duckdb_lock = duckdb_lock
        if session is not None:
            handle.session = session
        if file_lock is not None:
            handle.file_locks.append(file_lock)


def unregister_task_resource(task_id: str) -> None:
    """worker 正常退出时清理资源句柄（不调用 close/release，仅移除引用）。"""
    with _task_resources_lock:
        _task_resources.pop(task_id, None)


def get_task_resource(task_id: str) -> TaskResourceHandle | None:
    """查询任务当前注册的资源句柄（主要用于测试）。"""
    with _task_resources_lock:
        return _task_resources.get(task_id)


def _release_resource(handle: TaskResourceHandle) -> None:
    """释放单个 handle 上的所有资源，每步异常不掩盖前一步。"""
    # 1) 关闭 ThreadPoolExecutor
    if handle.executor is not None:
        try:
            shutdown = getattr(handle.executor, "shutdown", None)
            if callable(shutdown):
                # wait=False 避免阻塞取消流程
                shutdown(wait=False)
            else:
                close = getattr(handle.executor, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            logger.warning("Failed to shutdown executor: %s", exc, exc_info=False)
    # 2) 释放 DuckDB 单写锁
    if handle.duckdb_lock is not None:
        try:
            release = getattr(handle.duckdb_lock, "release", None)
            if callable(release):
                release()
            else:
                # RLock 没有 release 语义时尝试 release_all
                unlock = getattr(handle.duckdb_lock, "unlock", None)
                if callable(unlock):
                    # 仅在持有锁时释放，避免 RuntimeError
                    try:
                        if hasattr(handle.duckdb_lock, "_is_owned"):
                            if handle.duckdb_lock._is_owned():  # type: ignore[attr-defined]
                                unlock()
                        else:
                            unlock()
                    except RuntimeError:
                        pass
        except Exception as exc:
            logger.warning("Failed to release duckdb_lock: %s", exc, exc_info=False)
    # 3) 关闭额外 Session
    if handle.session is not None:
        try:
            close = getattr(handle.session, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            logger.warning("Failed to close session: %s", exc, exc_info=False)
    # 4) 释放文件锁
    for lock in handle.file_locks:
        try:
            release = getattr(lock, "release", None)
            if callable(release):
                release()
            else:
                close = getattr(lock, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            logger.warning("Failed to release file lock: %s", exc, exc_info=False)


def cancel_task_with_cleanup(task_id: str) -> AsyncTaskRead:
    """取消任务并释放其持有的所有资源。

    步骤：
    1. 调用现有 `cancel_async_task` 设置 status=cancelled
    2. 设置 cancel_requested=True（让 worker 自检时主动退出）
    3. 释放 _task_resources 中注册的资源（best-effort）
    """
    # 1) 取消任务（复用现有逻辑，终态保护由 cancel_async_task 内部完成）
    read = async_tasks.cancel_async_task(task_id)
    # 2) 标记 cancel_requested
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        try:
            async_tasks._set_task(db, task_id, cancel_requested=True)
        except Exception as exc:
            logger.warning(
                "Failed to set cancel_requested for task %s: %s",
                task_id, exc, exc_info=False,
            )
    finally:
        db.close()
    # 3) 释放资源
    with _task_resources_lock:
        handle = _task_resources.pop(task_id, None)
    if handle is not None:
        _release_resource(handle)
    # 重新读取最新状态返回
    return async_tasks.get_async_task(task_id) or read


# ── 3.7 恢复 resume_task ────────────────────────────────
def resume_task(task_id: str, worker_func: Callable[..., Any]) -> AsyncTaskRead:
    """从 interrupted/stalled 恢复任务，复用 batch_recovery 跳过已处理批次。

    worker_func 签名：`def worker_func(task_id: str, batch_recovery: list[dict] | None = None)`。
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            raise ValueError(f"Async task not found: {task_id}")
        if task.status not in (TaskState.INTERRUPTED.value, TaskState.STALLED.value):
            raise ValueError(
                f"Task {task_id} is not resumable (status={task.status})"
            )
        batch_recovery = async_tasks._json_loads(task.batch_recovery_json, None)
        # 状态转回 running，重置心跳/进度时间
        now = _now()
        updates: dict[str, Any] = {
            "status": TaskState.RUNNING.value,
            "stage": task.stage or TaskState.RUNNING.value,
            "heartbeat_at": now,
            "last_progress_at": now,
            "stage_started_at": now,
            "suggested_action": None,
            "cancel_requested": False,
        }
        async_tasks._set_task(db, task_id, **updates)
        db.refresh(task)
        read = async_tasks._task_to_read(task)
    finally:
        db.close()
    # 启动新 worker 线程，传入 batch_recovery
    async_tasks._start_worker(task_id, lambda tid: worker_func(tid, batch_recovery))
    return read
