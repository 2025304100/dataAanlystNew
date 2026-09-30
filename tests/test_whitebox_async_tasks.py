"""白盒测试 - 异步任务终态保护 (app.services.async_tasks)。

验证 _set_task 的终态保护逻辑，确保已 done/failed/cancelled 的任务
不被 worker 覆盖回 running 等状态（针对审查发现的并发保护问题）。
"""
from __future__ import annotations

import json

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


def test_task_to_read_accepts_object_recovery_metadata():
    """Partitioned sync tasks keep plan/cursor recovery as one JSON object."""
    recovery = {"plan": {"dataset": "fundamental"}, "processed": 0}
    task = AsyncTaskRecord(
        id="qa-recovery-object",
        task_type="external_sync_fundamental",
        status="queued",
        stage="queued",
        percent=0.0,
        message="created",
        # AsyncTaskRecord 的四个计数列是 NOT NULL + Python default=0，未 flush
        # 的对象上是 None；_task_to_read 走 pydantic 严格校验会拒 None。
        # 本用例只关心 batch_recovery 解析，按生产已落库形态补齐计数。
        total=0,
        processed=0,
        ok_count=0,
        failed_count=0,
        batch_recovery_json=json.dumps(recovery),
    )

    assert async_tasks._task_to_read(task).batch_recovery == recovery


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
    """_append_error 应只保留最近 20 条（且保持时间顺序）。"""
    task = AsyncTaskRecord(
        id="qa-err",
        task_type="market_data_sync",
        status="running",
        stage="sync",
        errors_json="[]",
    )
    db_session.add(task)
    db_session.commit()

    # 注意：_append_error 会过 `normalize_error`（前端 6 字段契约），非标准键
    # （如旧的 idx/msg）不会透传；自定义数据必须放 evidence 里才能存活。
    for i in range(25):
        async_tasks._append_error(task, {
            "code": "eval.test.err",
            "detail_zh": f"err-{i}",
            "evidence": {"idx": i},
        })

    import json
    errors = json.loads(task.errors_json)
    assert len(errors) == 20
    # 应保留最后 20 条（idx 5..24）
    assert errors[0]["evidence"]["idx"] == 5
    assert errors[-1]["evidence"]["idx"] == 24
    # 归一化后的 6 字段契约齐整（前端可直接渲染）
    assert all({"code", "severity", "category", "title_zh", "detail_zh"} <= set(e) for e in errors)


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


def _poison_session_via_failed_flush(db_session, task) -> None:
    """把 session 推进 PendingRollback —— 复现生产那条错的成因。

    必须是 **flush 失败**，普通语句报错不会污染 session（第一版用例就是这么写
    错的，剥掉守护照样绿）。生产原文是 "rolled back due to a previous exception
    during flush"，原始异常 `database is locked`；这里用 NOT NULL 违约稳定触发
    同样的状态。
    """
    task.status = None
    try:
        db_session.flush()
    except Exception:
        pass


def test_run_patrol_failure_leaves_session_readable(db_session, monkeypatch):
    """VIZ-0930-30：巡检写失败不能把调用方的 session 变成 PendingRollback。

    这条钉的是整轮套件里"红的位置每次都不一样"的真因：_run_patrol 声称
    best-effort，但异常只记日志、不回滚，于是失败的 flush 污染了 session，
    紧接着同 session 上的读（_expire_stale_tasks 的 SELECT、任务状态查询）
    连带抛 PendingRollbackError。
    """
    import app.services.task_state_machine as tsm

    task = _create_task(db_session, status="running", task_type="factor_pipeline")

    def _poison_then_raise(session):
        _poison_session_via_failed_flush(session, task)
        raise RuntimeError("patrol exploded")

    monkeypatch.setattr(tsm, "patrol_interrupted_and_stalled", _poison_then_raise)

    async_tasks._run_patrol(db_session)  # 不得向外抛

    reloaded = db_session.get(AsyncTaskRecord, task.id)
    assert reloaded is not None
    assert reloaded.status == "running"


def test_expire_stale_tasks_rolls_back_when_commit_fails(db_session, monkeypatch):
    """过期清理失败只能算"这轮没清"，不能污染读路径。"""
    from datetime import datetime, timedelta, timezone

    task = _create_task(db_session, status="running", task_type="universe_smart_sync")
    task.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=31)
    db_session.commit()

    def _fail_commit(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db_session, "commit", _fail_commit)
    async_tasks._expire_stale_tasks(db_session)  # 不得向外抛

    db_session.refresh(task)
    assert task.status == "running"


def test_get_async_task_survives_poisoned_patrol(db_session, monkeypatch):
    """状态查询入口：巡检把 session 弄脏后，仍必须读到任务而不是抛错。"""
    import app.services.task_state_machine as tsm

    task = _create_task(db_session, status="done", task_type="factor_pipeline")

    def _poison_then_raise(session):
        victim = session.get(AsyncTaskRecord, task.id)
        _poison_session_via_failed_flush(session, victim)
        raise RuntimeError("patrol exploded")

    monkeypatch.setattr(tsm, "patrol_interrupted_and_stalled", _poison_then_raise)

    read = async_tasks.get_async_task(task.id)
    assert read is not None
    assert read.id == task.id
    assert read.status == "done"


def test_interrupt_orphaned_async_tasks_marks_only_non_terminal(db_session):
    queued = _create_task(db_session, status="queued", task_type="factor_pipeline")
    running = _create_task(db_session, status="running", task_type="universe_smart_sync")
    done = _create_task(db_session, status="done", task_type="factor_pipeline")

    interrupted_ids = async_tasks.interrupt_orphaned_async_tasks(db_session)
    db_session.refresh(queued)
    db_session.refresh(running)
    db_session.refresh(done)

    assert set(interrupted_ids) == {queued.id, running.id}
    assert queued.status == running.status == "failed"
    assert queued.stage == running.stage == "interrupted"
    assert "Backend restarted" in queued.message
    assert done.status == "done"

    import json
    error = json.loads(queued.errors_json)[-1]
    assert error["code"] == "BACKEND_RESTART_INTERRUPTED"


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


# ══════════════════════════════════════════════════════
# PT-DEF-15：worker 线程的“库归属”守卫
# ══════════════════════════════════════════════════════

def test_worker_runs_when_db_url_unchanged(monkeypatch):
    """DB 没被重新指向时，worker 必须正常执行业务逻辑（守卫不误伤）。"""
    ran: list[str] = []
    monkeypatch.setattr(async_tasks, "_current_db_url", lambda: "sqlite:///same.db")

    async_tasks._start_worker("qa-guard-ok", lambda task_id: ran.append(task_id))
    thread = async_tasks._WORKER_THREADS.get("qa-guard-ok")
    if thread is not None:
        thread.join(timeout=2)

    assert ran == ["qa-guard-ok"]


def test_worker_aborts_when_db_repointed_after_spawn(monkeypatch):
    """PT-DEF-15：任务创建后进程 DB 被换掉 → worker 不得在“后来那张库”上跑业务。

    背景：worker 是 daemon 线程、跨请求存活，内部用 `get_session_local()` 的全局
    session。实测发现中心后台数据准备把“今天的空快照 + 一条 scan_run”写进了
    下一个请求/用例的库（体检报告 §十二.7）。
    """
    ran: list[str] = []
    urls = iter(["sqlite:///origin.db", "sqlite:///other.db"])
    monkeypatch.setattr(async_tasks, "_current_db_url", lambda: next(urls))

    async_tasks._start_worker("qa-guard-abort", lambda task_id: ran.append(task_id))
    thread = async_tasks._WORKER_THREADS.get("qa-guard-abort")
    if thread is not None:
        thread.join(timeout=2)

    assert ran == [], "DB 已被重新指向，worker 不应执行业务逻辑"


def test_current_db_url_survives_broken_factory(monkeypatch):
    """守卫不能因为“拿不到 URL”就把所有任务否掉：异常时返回 None → 不拦。

    兜底设计：`origin_db_url` 或 `current_db_url` 为空时都不触发 abort（宁可多跑
    不可错杀），避开因工厂未初始化就把整个异步链路卡死。
    """
    def _boom() -> str:
        raise RuntimeError("factory not ready")

    monkeypatch.setattr(async_tasks, "get_session_local", _boom)
    assert async_tasks._current_db_url() is None
