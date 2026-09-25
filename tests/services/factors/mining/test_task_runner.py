"""T13 · task_runner 提交入口 + worker 生命周期 + 双心跳（DoD）。

测试策略
========
- 全部走 `db_session` 的 SQLite 引擎：`create_async_task(use_control_plane=True)`
  会经 `get_control_session_local()` 造 NullPool 工厂，且**全局缓存** ——
  fixture 里重置缓存，使其基于当前测试库重建（否则会指向上一场测试的库）。
- worker 在守护线程里跑，测试用轮询等终态（超时即失败并附当前状态）。
- 端到端唤醒链：第一个任务阻塞在 stage → 第二个排队 → 解除阻塞 →
  第一个 finally 释放并**唤醒**第二个 → 两个都 done、双锁归零。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from app.services import async_tasks as AT
from app.services import task_state_machine as TSM
from app.services.factors.mining import task_lock as TL
from app.services.factors.mining import task_runner as TR

_TERMINAL = {"done", "failed", "cancelled"}


# ══════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════


@pytest.fixture
def mining_env(db_session, monkeypatch):
    """重置 control-plane 缓存 + 保证收工时 worker 全停。"""
    import app.db.session as session_mod

    # 让 control-plane 基于当前测试库重建（它有全局缓存，会指向上一个库）
    monkeypatch.setattr(session_mod, "_cp_factory", None)
    monkeypatch.setattr(session_mod, "_cp_engine", None)
    yield db_session
    AT.request_all_workers_stop()
    AT.wait_workers_stopped(timeout_seconds=15)
    # 🚨 WORKER_STOP_EVENT 是**模块级全局**，request_all_workers_stop 会 set 它；
    #    不清掉的话，本文件后续所有测试的 worker 都会立刻 cancelled。
    #    （第一版漏了这行：单跑绿、全文件跑时第 2 个测试起全部 cancelled。）
    AT.WORKER_STOP_EVENT.clear()


def _wait_terminal(db_session, task_id: str, timeout: float = 30.0) -> dict:
    """轮询到终态，返回**DB 记录**的 dict（含 result_json 等原始列）。

    ⚠️ 不能用 ：它返回 AsyncTaskRead DTO，
    **不含 result_json**（我第一版在这里栽了 —— 单跑时任务明明 done，
    断言却在 KeyError 上炸）。
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        # 🚨 worker 在**另一个线程的另一个 session**里提交；fixture session
        # 持有打开的事务时只能看到旧快照。每轮 expire_all 强制重读。
        db_session.expire_all()
        row = db_session.get(AT.AsyncTaskRecord, task_id)
        if row is None:
            raise AssertionError(f"任务 {task_id} 不存在")
        last = row
        if row.status in _TERMINAL:
            db_session.refresh(row)
            return {c.name: getattr(row, c.name)
                    for c in AT.AsyncTaskRecord.__table__.columns}
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 超时未到终态，最后状态={last.status}")


def _read_task(db_session, task_id: str):
    return db_session.get(AT.AsyncTaskRecord, task_id)


def _wait_locks_clear(db_session, timeout: float = 15.0) -> None:
    """worker 的 finally（释放锁）在置 done **之后**才跑完 ——
    轮询到终态就断言锁会撞上竞态窗口（全量回归时真实命中过）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        db_session.expire_all()
        locks = _locks(db_session)
        if not locks.mining_domain.get("busy") and not locks.duckdb_write.get("busy"):
            return
        time.sleep(0.05)


def _task_brief(db_session, task_id: str) -> dict:
    t = _read_task(db_session, task_id)
    return {"status": t.status, "stage": t.stage, "message": t.message}


def _locks(db_session) -> TL.LockStatus:
    return TL.get_lock_status(db_session)


# ══════════════════════════════════════════════════════════
# 1. 提交：正常路径
# ══════════════════════════════════════════════════════════


class TestSubmitHappyPath:
    def test_submit_creates_task_acquires_locks_and_completes(
        self, mining_env, db_session
    ):
        result = TR.submit_mining_run(
            {"run_id": "run-1", "generations": 3}, run_id="run-1",
            stage_runner=TR.skeleton_stage_runner,
        )
        assert result.queue_position == 0
        assert result.started is True

        read = _wait_terminal(db_session, result.task_id)
        assert read["status"] == "done"
        assert read["result_json"]
        # 默认骨架（M1a）只回验证信息；run_id 的回传由真实 GA（T22/T23）负责
        assert "M1a" in read["result_json"]

        # 双锁必须全部释放（finally 保证；有竞态窗口，需轮询）
        _wait_locks_clear(db_session)
        locks = _locks(db_session)
        assert locks.mining_domain["busy"] is False
        assert locks.duckdb_write["busy"] is False
        assert locks.duckdb_write["queue"] == []

    def test_result_shape(self, mining_env, db_session):
        result = TR.submit_mining_run({"run_id": "run-shape"}, run_id="run-shape",
                                       stage_runner=TR.skeleton_stage_runner)
        _wait_terminal(db_session, result.task_id)
        d = result.to_dict()
        for key in ("task_id", "run_id", "status", "queue_position", "started",
                    "started_hint_zh"):
            assert key in d

    def test_stage_runner_receives_context_and_result_lands(self, mining_env,
                                                            db_session):
        seen: dict = {}

        def stage(ctx: TR.MiningWorkerContext) -> dict:
            seen["task_id"] = ctx.task_id
            seen["run_id"] = ctx.run_id
            seen["payload_run_id"] = ctx.payload.get("run_id")
            seen["holds_write"] = ctx.holds_duckdb_write
            ctx.heartbeat()          # 阶段内心跳不应报错
            assert ctx.should_stop() is False
            return {"message_zh": "自定义阶段完成", "generations": 7}

        result = TR.submit_mining_run({"run_id": "run-ctx"}, run_id="run-ctx",
                                      stage_runner=stage)
        _wait_terminal(db_session, result.task_id)

        assert seen["task_id"] == result.task_id
        assert seen["run_id"] == "run-ctx"
        assert seen["payload_run_id"] == "run-ctx"
        assert seen["holds_write"] is True

        db_session.expire_all()
        t = _read_task(db_session, result.task_id)
        import json

        payload_back = json.loads(t.result_json)
        assert payload_back["generations"] == 7
        assert "自定义阶段完成" in t.message


# ══════════════════════════════════════════════════════════
# 2. mining_domain：忙时拒绝且**不创建任务**
# ══════════════════════════════════════════════════════════


class TestMiningDomainGate:
    def test_busy_rejects_without_creating_task(self, mining_env, db_session):
        # 先手动占住挖掘域
        TL.acquire_mining_lock(db_session, task_id="manual-owner", run_id="r0")
        before = db_session.query(AT.AsyncTaskRecord).count()

        with pytest.raises(TL.MiningDomainBusy) as ei:
            TR.submit_mining_run({"run_id": "run-x"}, run_id="run-x")
        assert ei.value.owner_task_id == "manual-owner"

        # 需求：本次提交被拒绝，**未创建任务**
        after = db_session.query(AT.AsyncTaskRecord).count()
        assert after == before, "mining_domain 忙时不应创建任务记录"

        TL.release_lock(db_session, lock_key=TL.LOCK_MINING_DOMAIN,
                        task_id="manual-owner")

    def test_race_fallback_marks_task_failed(self, mining_env, db_session,
                                             monkeypatch):
        """peek 通过但 acquire 被抢（竞态）→ 刚建的任务标 failed，仍抛 Busy。"""
        calls = {"n": 0}
        real_status = TL.get_lock_status

        def peek_passes_then_lose(db):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一次 peek：假装空闲
                return TL.LockStatus(
                    mining_domain={"busy": False},
                    duckdb_write={"busy": False, "queue": []},
                )
            return real_status(db)

        monkeypatch.setattr(TR.TL, "get_lock_status", peek_passes_then_lose)
        # 再让真正的 acquire 必然失败：先占住
        TL.acquire_mining_lock(db_session, task_id="race-owner", run_id="r0")

        with pytest.raises(TL.MiningDomainBusy):
            TR.submit_mining_run({"run_id": "run-race"}, run_id="run-race")

        # 刚建的任务被标 failed（留痕，不是无声消失）
        rows = db_session.query(AT.AsyncTaskRecord).filter(
            AT.AsyncTaskRecord.task_type == TR.TASK_TYPE).all()
        assert rows, "竞态路径应有任务记录"
        assert all(r.status == "failed" for r in rows)
        assert all("MINING_DOMAIN_BUSY" in (r.message or "") for r in rows)

        TL.release_lock(db_session, lock_key=TL.LOCK_MINING_DOMAIN,
                        task_id="race-owner")


# ══════════════════════════════════════════════════════════
# 3. duckdb_write：排队 + 唤醒链
# ══════════════════════════════════════════════════════════


class TestDuckDBWriteQueueing:
    def test_busy_write_queues_without_starting_worker(
        self, mining_env, db_session
    ):
        """第一个任务阻塞在 stage → 第二个排队（不启动 worker）→
        解除阻塞 → 第一个 finally 释放并**唤醒**第二个 → 两个都 done。"""
        blocker = threading.Event()
        entered_first = threading.Event()

        def stage(ctx: TR.MiningWorkerContext) -> dict:
            if ctx.payload.get("run_id") == "run-1":
                entered_first.set()
                blocker.wait(timeout=30)          # 阻塞第一个任务
            return {"message_zh": f"done {ctx.payload.get('run_id')}"}

        r1 = TR.submit_mining_run({"run_id": "run-1"}, run_id="run-1",
                                  stage_runner=stage)
        assert r1.started is True
        assert entered_first.wait(timeout=15), "第一个任务未进入 stage"

        # ⚠️ 场景澄清：mining_domain **全局单任务**，第二个挖掘任务会被 409
        #    拒绝（架构上不可能出现「两个挖掘任务排队」）。
        #    duckdb_write 的排队是给**不占挖掘域的任务类型**用的
        #    （§6.5：字段校验任务不占 mining_domain）。故这里手工构造一个
        #    「只排 duckdb_write 的 queued 任务」来验证唤醒链。
        r2 = AT.create_async_task(TR.TASK_TYPE, {"run_id": "run-2"},
                                  use_control_plane=True)
        TL.acquire_write_slot(db_session, task_id=r2.id, run_id="run-2")
        assert _read_task(db_session, r2.id).status == "queued"

        # 解除阻塞 → r1 finally 释放 duckdb_write → 唤醒 r2 → 两个都 done
        blocker.set()
        read1 = _wait_terminal(db_session, r1.task_id, timeout=40)
        read2 = _wait_terminal(db_session, r2.id, timeout=40)
        assert read1["status"] == "done"
        assert read2["status"] == "done"

        _wait_locks_clear(db_session)
        locks = _locks(db_session)
        assert locks.mining_domain["busy"] is False
        assert locks.duckdb_write["busy"] is False
        assert locks.duckdb_write["queue"] == []

    def test_worker_transitions_queued_to_running(self, mining_env, db_session):
        result = TR.submit_mining_run({"run_id": "run-t"}, run_id="run-t",
                                      stage_runner=TR.skeleton_stage_runner)
        read = _wait_terminal(db_session, result.task_id)
        assert read["status"] == "done"
        # started_at 已由状态机补齐
        db_session.expire_all()
        t = _read_task(db_session, result.task_id)
        assert t.started_at is not None


# ══════════════════════════════════════════════════════════
# 4. 双心跳
# ══════════════════════════════════════════════════════════


class TestHeartbeat:
    def test_heartbeat_mining_refreshes_task_and_locks(self, mining_env,
                                                       db_session):
        TL.acquire_mining_lock(db_session, task_id="hb-task", run_id="r")
        TL.acquire_write_slot(db_session, task_id="hb-task", run_id="r")
        # 任务记录（running 状态才能刷）
        db_session.add(AT.AsyncTaskRecord(
            id="hb-task", task_type=TR.TASK_TYPE, status="running",
            stage="running", percent=0, message="x", payload_json="{}",
            is_terminal_locked=0,
        ))
        db_session.commit()

        before_lock = _locks(db_session)
        before_task = _read_task(db_session, "hb-task").heartbeat_at

        time.sleep(0.01)
        TR.heartbeat_mining("hb-task")

        after_lock = _locks(db_session)
        after_task = _read_task(db_session, "hb-task").heartbeat_at
        assert after_lock.duckdb_write["heartbeatAt"] >= \
            before_lock.duckdb_write["heartbeatAt"]
        assert after_lock.mining_domain["heartbeatAt"] >= \
            before_lock.mining_domain["heartbeatAt"]
        assert after_task is not None
        if before_task is not None:
            assert after_task >= before_task

        # 终态任务不刷心跳（避免已结束任务被 patrol 复活）
        db_session.get(AT.AsyncTaskRecord, "hb-task").status = "done"
        db_session.commit()
        TR.heartbeat_mining("hb-task")       # 不应抛错，也不应改动
        assert _read_task(db_session, "hb-task").heartbeat_at == after_task

        # 清理
        TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE, task_id="hb-task")
        TL.release_lock(db_session, lock_key=TL.LOCK_MINING_DOMAIN, task_id="hb-task")

    def test_heartbeat_pump_covers_both_timeouts(self):
        """一个泵刷两处：泵每次触发间隔 15s，必须同时小于两个超时。"""
        # 我第一版断言写成「间隔乘 60 < 超时」，纯属算错单位
        assert TR.TASK_HEARTBEAT_SECONDS < TSM.HEARTBEAT_TIMEOUT.total_seconds()
        assert TR.TASK_HEARTBEAT_SECONDS < TL.HEARTBEAT_TIMEOUT


# ══════════════════════════════════════════════════════════
# 5. 取消 / 恢复
# ══════════════════════════════════════════════════════════


class TestCancelAndResume:
    def test_global_stop_cancels_before_stages(self, mining_env, db_session):
        AT.request_all_workers_stop()
        try:
            result = TR.submit_mining_run({"run_id": "run-stop"}, run_id="run-stop",
                                          stage_runner=TR.skeleton_stage_runner)
            read = _wait_terminal(db_session, result.task_id)
            assert read["status"] == "cancelled"
        finally:
            AT.WORKER_STOP_EVENT.clear()

    def test_resume_pending_mining_tasks(self, mining_env, db_session):
        """滞留 queued 的任务：若已持有 duckdb_write → 补启动 worker。"""
        # 模拟滞留：手动建 queued 任务 + 让它持有 duckdb_write
        # （绕过 submit：直接用底层 API 造现场）
        task = AT.create_async_task(TR.TASK_TYPE, {"run_id": "run-stuck"},
                                    use_control_plane=True)
        TL.acquire_mining_lock(db_session, task_id=task.id, run_id="run-stuck")
        TL.acquire_write_slot(db_session, task_id=task.id, run_id="run-stuck")

        resumed = TR.resume_pending_mining_tasks(
            stage_runner=TR.skeleton_stage_runner)
        assert task.id in resumed

        read = _wait_terminal(db_session, task.id)
        assert read["status"] == "done"
        _wait_locks_clear(db_session)
        locks = _locks(db_session)
        assert locks.mining_domain["busy"] is False
        assert locks.duckdb_write["busy"] is False

    def test_resume_skips_tasks_without_lock(self, mining_env, db_session):
        AT.create_async_task(TR.TASK_TYPE, {"run_id": "run-nolock"},
                             use_control_plane=True)
        resumed = TR.resume_pending_mining_tasks(
            stage_runner=TR.skeleton_stage_runner)
        assert resumed == []


# ══════════════════════════════════════════════════════════
# 6. 失败路径：stage_runner 抛异常 → failed 且锁仍释放
# ══════════════════════════════════════════════════════════


class TestFailurePath:
    def test_stage_runner_exception_fails_task_and_releases_locks(
        self, mining_env, db_session
    ):
        def bad_stage(ctx):
            ctx.heartbeat()
            raise RuntimeError("GA exploded")

        result = TR.submit_mining_run({"run_id": "run-bad"}, run_id="run-bad",
                                      stage_runner=bad_stage)
        read = _wait_terminal(db_session, result.task_id)
        assert read["status"] == "failed"

        # finally 必须释放双锁（否则锁泄漏会卡死后续所有提交）
        _wait_locks_clear(db_session)
        locks = _locks(db_session)
        assert locks.mining_domain["busy"] is False
        assert locks.duckdb_write["busy"] is False

    def test_domain_free_after_failure_and_next_submit_succeeds(
        self, mining_env, db_session
    ):
        """失败必须释放双锁 → 域空闲 → 下一次提交照常成功。

        （第一版想测「两个挖掘任务排队」，但 mining_domain 全局单任务，
        第二个提交会被 409 拒绝 —— 架构上就不存在这个场景，是测试设计错。）
        """
        blocker = threading.Event()
        entered = threading.Event()

        def stage(ctx):
            entered.set()
            blocker.wait(timeout=30)
            raise RuntimeError("first failed")

        r1 = TR.submit_mining_run({"run_id": "run-f1"}, run_id="run-f1",
                                  stage_runner=stage)
        assert entered.wait(timeout=15)
        blocker.set()

        read1 = _wait_terminal(db_session, r1.task_id, timeout=40)
        assert read1["status"] == "failed"

        # 域已空闲：新提交照常走完整生命周期
        r2 = TR.submit_mining_run({"run_id": "run-f2"}, run_id="run-f2",
                                  stage_runner=TR.skeleton_stage_runner)
        assert r2.started is True
        read2 = _wait_terminal(db_session, r2.task_id, timeout=40)
        assert read2["status"] == "done"

        _wait_locks_clear(db_session)
        locks = _locks(db_session)
        assert locks.mining_domain["busy"] is False
        assert locks.duckdb_write["busy"] is False


# ══════════════════════════════════════════════════════════
# 7. DEF-12：代内推进 touch + cancel_requested 感知
# ══════════════════════════════════════════════════════════


class TestDef12ProgressTouchAndCancel:
    def test_touch_run_progress_refreshes_updated_at(self, mining_env,
                                                     db_session):
        """代内评估周期性 touch updated_at（看门狗推进信号），且带节流。"""
        from app.models.factor_mining import FactorMiningRun

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db_session.add(FactorMiningRun(
            id="run-def12", status="running",
            candidate_pool_snapshot_id="snap-def12",
            data_cutoff_at=now, start_date=now, end_date=now,
            rebalance_frequency="daily", max_generation=5,
            created_at=now, updated_at=now,
        ))
        db_session.commit()
        old_ts = db_session.get(FactorMiningRun, "run-def12").updated_at

        assert TR.touch_run_progress("run-def12", min_interval=0.0) is True
        db_session.expire_all()
        new_ts = db_session.get(FactorMiningRun, "run-def12").updated_at
        assert new_ts is not None and new_ts >= old_ts

        # 节流窗口内的第二次调用必须短路（不给 DB 压力）
        assert TR.touch_run_progress("run-def12", min_interval=3600) is False

    def test_cancel_requested_makes_should_stop_true(self, mining_env,
                                                     db_session, monkeypatch):
        """cancel_async_task 写入 cancel_requested=1 后，ctx.should_stop() 变 True。"""
        task = AT.create_async_task(TR.TASK_TYPE, {"run_id": "run-cancel12"},
                                    use_control_plane=True)
        ctx = TR.MiningWorkerContext(task_id=task.id, run_id="run-cancel12",
                                     payload={}, holds_duckdb_write=True)
        monkeypatch.setattr(TR, "_cancel_state", {})  # 清节流缓存
        assert ctx.should_stop() is False

        AT.cancel_async_task(task.id)
        monkeypatch.setattr(TR, "_cancel_state", {})
        assert ctx.should_stop() is True

        # 终态任务的 cancel 不再翻转（幂等：已 cancelled 时 should_stop 仍 True 无妨，
        # 但任务记录不存在时必须保守返回 False，防误杀直跑/测试注入场景）
        monkeypatch.setattr(TR, "_cancel_state", {})
        assert TR._cancel_requested_cached("no-such-task-id") is False
