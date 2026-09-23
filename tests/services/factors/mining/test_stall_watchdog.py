"""停滞 run 看门狗（stall watchdog）· 回归哨兵（2026-09-23 实测缺陷）。

**缺陷机制**（真实复现）：
worker 在初代评估阶段卡死（无日志、无异常、无超时），但**心跳线程仍在刷新**心跳 ——
于是 `heartbeat_at` 永远新鲜，`expire_stale_locks` 按心跳超时永远回收不了这把锁：

    locks: {"miningDomain": {"busy": true, "runId": "f1cbd75d…",
             "heartbeatAt": "01:31:56"}}   ← 当前时间 01:31:58，心跳 2 秒前
    runs:  f1cbd75d cancelled(已被用户 discard) / 另一个 running 已挂 18 小时

后果：**整个挖掘功能对新提交关闭**（`POST /runs` → 409 MINING_DOMAIN_BUSY），
且用户无论如何操作都无法恢复。

**修复思路**：锁回收按「心跳」判定，救不了"卡死但心跳还活着"；必须补一条按
**任务推进**判定的看门狗 —— run 处于 running 且长时间无代数推进（`updated_at` 不动）
→ 置 failed(STALLED) 并释放它占的锁。

四条哨兵：
1. 停滞 running → 置 failed 且**释放其锁**（解锁功能入口）
2. 有推进的 running → 不动（别误杀正在跑的实验）
3. 终态 run → 不动（幂等，别重复处理历史）
4. 只释放该 run 自己的锁（不得误放他人锁）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.factor_mining import FactorMiningRun
from app.services.factors.mining import stall_watchdog as WD
from app.services.factors.mining import task_lock as TL


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_run(db, run_id: str, *, status: str, age_minutes: int, generation: int = 0,
              max_generation: int = 20):
    now = _utcnow()
    ts = now - timedelta(minutes=age_minutes)
    row = FactorMiningRun(
        id=run_id,
        status=status,
        candidate_pool_snapshot_id="snap-x",
        data_cutoff_at=ts,
        start_date=ts,
        end_date=ts,
        current_generation=generation,
        max_generation=max_generation,
        created_at=ts,
        updated_at=ts,
    )
    db.add(row)
    db.commit()
    return row


def _hold_lock(db, *, task_id: str, run_id: str):
    TL.acquire_mining_lock(db, task_id=task_id, run_id=run_id)


@pytest.fixture()
def factory(db_session):
    engine = db_session.get_bind()
    return sessionmaker(bind=engine)


def test_stalled_running_is_failed_and_unlocked(db_session, factory):
    """哨兵 1：停滞的 running 被置 failed，并且它占的锁被释放（功能入口恢复）。"""
    _make_run(db_session, "run-stalled", status="running", age_minutes=30)
    _hold_lock(db_session, task_id="task-stalled", run_id="run-stalled")
    assert TL.get_lock_status(db_session).mining_domain["busy"] is True

    reaped = WD.reap_stalled_runs(db_session, threshold_seconds=600)

    assert "run-stalled" in reaped
    row = db_session.get(FactorMiningRun, "run-stalled")
    assert row.status == "failed"
    # 表里只有 error_code（无 message 列）——用户可读文案由 API/前端按 error_code 映射
    assert row.error_code == WD.ERROR_CODE_STALLED
    assert TL.get_lock_status(db_session).mining_domain["busy"] is False


def test_fresh_running_is_kept(db_session, factory):
    """哨兵 2：刚有推进的 running 不得被误杀。"""
    _make_run(db_session, "run-fresh", status="running", age_minutes=1, generation=3)
    _hold_lock(db_session, task_id="task-fresh", run_id="run-fresh")

    reaped = WD.reap_stalled_runs(db_session, threshold_seconds=600)

    assert reaped == []
    assert db_session.get(FactorMiningRun, "run-fresh").status == "running"
    assert TL.get_lock_status(db_session).mining_domain["busy"] is True


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "converged", "draft"])
def test_terminal_runs_untouched(db_session, factory, status):
    """哨兵 3：终态/未启动的 run 不处理（幂等，别动历史）。"""
    _make_run(db_session, "run-done", status=status, age_minutes=600)

    assert WD.reap_stalled_runs(db_session, threshold_seconds=600) == []
    assert db_session.get(FactorMiningRun, "run-done").status == status


def test_only_own_lock_released(db_session, factory):
    """哨兵 4：只释放被判停滞的那个 run 的锁，不误放他人。"""
    _make_run(db_session, "run-stalled", status="running", age_minutes=30)
    _hold_lock(db_session, task_id="task-stalled", run_id="run-stalled")

    # 另一把锁属于别的 run（duckdb_write 队列场景）
    _make_run(db_session, "run-other", status="running", age_minutes=1)
    db_session.expunge_all()
    TL.acquire_write_slot(db_session, task_id="task-other", run_id="run-other")

    WD.reap_stalled_runs(db_session, threshold_seconds=600)

    status = TL.get_lock_status(db_session)
    assert status.mining_domain["busy"] is False, "停滞 run 的域锁应被释放"
    assert status.duckdb_write["busy"] is True, "他人（新鲜 run）的写锁不得被误放"
    assert status.duckdb_write["taskId"] == "task-other"


def test_find_stalled_runs_is_pure(db_session, factory):
    """`find_stalled_runs` 只报告不修改（供上层预览/报警）。"""
    _make_run(db_session, "run-stalled", status="running", age_minutes=30)

    found = WD.find_stalled_runs(db_session, threshold_seconds=600)

    assert found == ["run-stalled"]
    assert db_session.get(FactorMiningRun, "run-stalled").status == "running"


# ══════════════════════════════════════════════════════════
# 孤儿锁：run 已是终态（或不存在）却仍持锁 —— 实测现场形态
# ══════════════════════════════════════════════════════════


def test_lock_held_by_cancelled_run_is_released(db_session, factory):
    """哨兵 5：run 已被 discard（cancelled）但锁没放 → 孤儿锁必须释放。

    这是实测现场：`f1cbd75d` discard 后 worker 未停、锁未放，导致后续
    `POST /runs` 一直 409 —— 而它**不是 running**，停滞检测覆盖不到。
    """
    _make_run(db_session, "run-cancelled", status="cancelled", age_minutes=5)
    _hold_lock(db_session, task_id="task-cancelled", run_id="run-cancelled")

    released = WD.release_orphan_locks(db_session)

    assert "run-cancelled" in released
    assert TL.get_lock_status(db_session).mining_domain["busy"] is False


@pytest.mark.parametrize("status", ["succeeded", "failed", "converged"])
def test_lock_held_by_other_terminal_statuses_released(db_session, factory, status):
    """哨兵 6：其他终态同样不该持锁。"""
    _make_run(db_session, "run-term", status=status, age_minutes=5)
    _hold_lock(db_session, task_id="task-term", run_id="run-term")

    assert "run-term" in WD.release_orphan_locks(db_session)
    assert TL.get_lock_status(db_session).mining_domain["busy"] is False


def test_lock_of_running_run_not_touched(db_session, factory):
    """哨兵 7：running 批次正常持锁，不得被孤儿清理误放。"""
    _make_run(db_session, "run-live", status="running", age_minutes=1)
    _hold_lock(db_session, task_id="task-live", run_id="run-live")

    assert WD.release_orphan_locks(db_session) == []
    assert TL.get_lock_status(db_session).mining_domain["busy"] is True


def test_lock_of_missing_run_is_released(db_session, factory):
    """哨兵 8：锁指向一个不存在的 run（脏数据）→ 释放，避免永久堵门。"""
    _hold_lock(db_session, task_id="task-ghost", run_id="run-does-not-exist")

    assert "run-does-not-exist" in WD.release_orphan_locks(db_session)
    assert TL.get_lock_status(db_session).mining_domain["busy"] is False
