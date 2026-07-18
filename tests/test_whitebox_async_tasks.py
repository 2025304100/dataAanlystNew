"""白盒测试 - 异步任务终态保护 (app.services.async_tasks)。

验证 _set_task 的终态保护逻辑，确保已 done/failed/cancelled 的任务
不被 worker 覆盖回 running 等状态（针对审查发现的并发保护问题）。
"""
from __future__ import annotations

import pytest

from app.models.async_task import AsyncTaskRecord
from app.services import async_tasks

pytestmark = pytest.mark.whitebox


def _create_task(db_session, status="queued", task_type="market_data_sync") -> AsyncTaskRecord:
    task = AsyncTaskRecord(
        id=f"qa-task-{id(db_session)}-{status}",
        task_type=task_type,
        status=status,
        stage="prepare",
        percent=0.0,
        message="",
        total=10,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------- _task_to_dict 序列化 ----------

def test_task_to_dict_handles_null_json():
    """errors_json/result_json 为 NULL 时应安全降级。"""
    task = AsyncTaskRecord(
        id="qa-1",
        task_type="market_data_sync",
        status="done",
        stage="done",
        percent=100.0,
        errors_json=None,
        result_json=None,
    )
    d = async_tasks._task_to_dict(task)
    assert d["errors"] == []
    assert d["result"] is None
    assert d["percent"] == 100.0


def test_task_timestamps_are_serialized_as_explicit_utc():
    from datetime import datetime, timezone

    task = AsyncTaskRecord(
        id="qa-utc",
        task_type="factor_pipeline",
        status="running",
        stage="mirror",
        percent=5,
        message="working",
        total=10,
        processed=1,
        ok_count=0,
        failed_count=0,
        created_at=datetime(2026, 7, 17, 4, 0),
        updated_at=datetime(2026, 7, 17, 4, 1),
    )
    payload = async_tasks._task_to_read(task).model_dump(mode="json")

    assert payload["created_at"].endswith("Z")
    assert payload["updated_at"].endswith("Z")
    assert async_tasks._as_utc(task.created_at).tzinfo == timezone.utc


def test_json_loads_fallback():
    """_json_loads 在解析失败时应返回 fallback。"""
    assert async_tasks._json_loads(None, []) == []
    assert async_tasks._json_loads("not json", {}) == {}
    assert async_tasks._json_loads("[1, 2]", []) == [1, 2]


# ---------- 终态保护 ----------

def test_set_task_updates_running_to_done(db_session):
    """running → done 应正常更新。"""
    task = _create_task(db_session, status="running")
    updated = async_tasks._set_task(db_session, task.id, status="done", stage="done", percent=100.0)
    assert updated.status == "done"
    assert updated.percent == 100.0


def test_set_task_protects_done_from_status_overwrite(db_session):
    """[终态保护] done 状态不应被覆盖回 running。"""
    task = _create_task(db_session, status="done")
    # 尝试用 worker 把它改回 running
    updated = async_tasks._set_task(db_session, task.id, status="running", stage="sync", percent=50.0)
    assert updated.status == "done", "已 done 的任务不应被 worker 覆盖回 running"
    # 但非状态字段（如 message）应仍可更新
    updated = async_tasks._set_task(db_session, task.id, message="worker attempted overwrite")
    db_session.refresh(task)
    assert task.message == "worker attempted overwrite"


def test_set_task_protects_failed_from_status_overwrite(db_session):
    """[终态保护] failed 状态不应被覆盖。"""
    task = _create_task(db_session, status="failed")
    updated = async_tasks._set_task(db_session, task.id, status="running")
    assert updated.status == "failed"


def test_set_task_protects_cancelled_from_status_overwrite(db_session):
    """[终态保护] cancelled 状态不应被 worker 覆盖为 done。

    这是用户取消任务后，worker 仍尝试标记 done 的典型竞态场景。
    """
    task = _create_task(db_session, status="cancelled")
    updated = async_tasks._set_task(db_session, task.id, status="done", stage="done", percent=100.0)
    assert updated.status == "cancelled", "已 cancelled 的任务不应被 worker 覆盖为 done"


def test_set_task_non_terminal_state_allows_overwrite(db_session):
    """queued → running 应允许更新（非终态可流转）。"""
    task = _create_task(db_session, status="queued")
    updated = async_tasks._set_task(db_session, task.id, status="running", stage="sync", percent=10.0)
    assert updated.status == "running"
    assert updated.percent == 10.0


def test_set_task_unknown_task_raises(db_session):
    """不存在的 task_id 应抛 ValueError。"""
    with pytest.raises(ValueError, match="Async task not found"):
        async_tasks._set_task(db_session, "nonexistent-id", status="running")


# ---------- 错误追加 ----------

def test_append_error_keeps_last_20(db_session):
    """_append_error 应只保留最近 20 条错误。"""
    task = AsyncTaskRecord(
        id="qa-err",
        task_type="market_data_sync",
        status="running",
        stage="sync",
        errors_json="[]",
    )
    db_session.add(task)
    db_session.commit()

    for i in range(25):
        async_tasks._append_error(task, {"idx": i, "msg": f"err-{i}"})

    import json
    errors = json.loads(task.errors_json)
    assert len(errors) == 20
    # 应保留最后 20 条（idx 5..24）
    assert errors[0]["idx"] == 5
    assert errors[-1]["idx"] == 24


# ---------- 过期任务清理 ----------

def test_expire_stale_tasks_marks_running_as_failed(db_session):
    """[L-5/超时] 超过 30 分钟未更新的 running 任务应被标记为 failed。"""
    from datetime import datetime, timedelta, timezone
    task = AsyncTaskRecord(
        id="qa-stale",
        task_type="market_data_sync",
        status="running",
        stage="sync",
        percent=50.0,
    )
    # 将 updated_at 设为 31 分钟前
    task.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=31)
    db_session.add(task)
    db_session.commit()

    async_tasks._expire_stale_tasks(db_session)
    db_session.refresh(task)
    assert task.status == "failed"
    assert task.stage == "failed"
    assert "expired" in task.message.lower()


# ============================================================================
# discovery_tasks 稳定性常量守护（P0 回归：防止超时常量被意外修改）
# ============================================================================

def test_stale_running_deadline_now_10_minutes():
    """【P0 稳定性回归】STALE_RUNNING_DEADLINE 应为 10 分钟。

    历史事件：原值 30 分钟太长，卡死任务需 30 分钟才被清理。配合 watchdog 心跳
    （每 30s 更新 updated_at）+ 单 symbol 90s 超时，10 分钟足够区分卡死与正常运行。
    """
    from datetime import timedelta
    from app.services.discovery_tasks import STALE_RUNNING_DEADLINE

    assert STALE_RUNNING_DEADLINE == timedelta(minutes=10), (
        f"STALE_RUNNING_DEADLINE 应为 10 分钟, 实际: {STALE_RUNNING_DEADLINE}"
    )


def test_sync_one_symbol_timeout_constant_is_90():
    """【P0 稳定性回归】SYNC_ONE_SYMBOL_TIMEOUT_SECONDS 应为 90 秒。

    覆盖 _fetch_history 3 次重试（每次最多 20s = 连接 5s + 读取 15s）+ 退避 sleep + 余量。
    超时后跳过该 symbol，记录 failed_count，继续下一个，避免单 symbol 卡死阻塞整个任务。
    """
    from app.services.discovery_tasks import SYNC_ONE_SYMBOL_TIMEOUT_SECONDS

    assert SYNC_ONE_SYMBOL_TIMEOUT_SECONDS == 90, (
        f"SYNC_ONE_SYMBOL_TIMEOUT_SECONDS 应为 90, 实际: {SYNC_ONE_SYMBOL_TIMEOUT_SECONDS}"
    )


def test_watchdog_heartbeat_constant_is_30():
    """【P0 稳定性回归】_WATCHDOG_HEARTBEAT_SECONDS 应为 30 秒。

    每 30s 更新 task.updated_at，防止 _expire_stale_tasks 误判正常任务为 stale。
    真正的卡死由单 symbol 超时（90s）兜底，watchdog 只防止误判。
    """
    from app.services.discovery_tasks import _WATCHDOG_HEARTBEAT_SECONDS

    assert _WATCHDOG_HEARTBEAT_SECONDS == 30, (
        f"_WATCHDOG_HEARTBEAT_SECONDS 应为 30, 实际: {_WATCHDOG_HEARTBEAT_SECONDS}"
    )


def test_resume_deadline_is_1_day():
    """【P0 稳定性回归】RESUME_DEADLINE 应为 1 天。

    暂停任务 24 小时内可恢复，超时标记 expired。
    """
    from datetime import timedelta
    from app.services.discovery_tasks import RESUME_DEADLINE

    assert RESUME_DEADLINE == timedelta(days=1), (
        f"RESUME_DEADLINE 应为 1 天, 实际: {RESUME_DEADLINE}"
    )


def test_discovery_terminal_states_constant():
    """【P0 稳定性回归】_TERMINAL_STATES 应包含 done/failed/cancelled/expired。

    终态保护：已终态的任务不允许被 worker 覆盖状态/阶段（防止取消后 worker 仍标记 done）。
    """
    from app.services.discovery_tasks import _TERMINAL_STATES

    assert _TERMINAL_STATES == ("done", "failed", "cancelled", "expired"), (
        f"_TERMINAL_STATES 应为 (done, failed, cancelled, expired), 实际: {_TERMINAL_STATES}"
    )


def test_expire_stale_tasks_skips_recent_running(db_session):
    """刚更新的 running 任务不应被误标记为 failed。"""
    task = AsyncTaskRecord(
        id="qa-fresh",
        task_type="market_data_sync",
        status="running",
        stage="sync",
        percent=10.0,
    )
    db_session.add(task)
    db_session.commit()

    async_tasks._expire_stale_tasks(db_session)
    db_session.refresh(task)
    assert task.status == "running"
