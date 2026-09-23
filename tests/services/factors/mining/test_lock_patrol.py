"""挖据锁巡检（patrol）· 回归哨兵。

背景（2026-09-22 实测）：`task_lock.expire_stale_locks()` 已实现且语义正确，
但**全仓没有任何调用方** —— 后端进程被强杀/重启后，锁行永久残留，
界面 Step4 提交恒被拒（弹窗「已有挖掘任务进行中」），用户无自救入口。

本卡补一个可调度入口 `patrol_once()` 并由 `main.lifespan` 起后台循环调用。

三条哨兵（每条都是"修错了就会红"）：
1. 心跳超时的锁必须被回收（僵尸锁 → 功能可用）
2. 心跳新鲜的锁**绝不能**被误杀（否则正在跑的实验会被打断）
3. 回收 `duckdb_write` 时不得丢掉排队队列（T12 已修缺陷的回归哨兵）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.factors.mining import task_lock as TL


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def factory(db_session):
    """把 pytest 的 db_session 包装成 session_factory，注入 patrol_once。

    这样 patrol 走的是真实代码路径（自己开 session、自己回收），
    但不依赖 DatabaseManager 是否初始化。
    """
    from sqlalchemy.orm import sessionmaker

    engine = db_session.get_bind()
    return sessionmaker(bind=engine)


def _make_stale_lock(db, *, task_id: str, minutes: int = 60):
    """构造一把心跳超时的锁（模拟进程被强杀后残留）。"""
    from app.models.task_lock import TaskLock

    now = _utcnow()
    db.add(
        TaskLock(
            lock_key=TL.LOCK_MINING_DOMAIN,
            owner_task_id=task_id,
            owner_run_id=f"run-{task_id}",
            acquired_at=now - timedelta(minutes=minutes),
            heartbeat_at=now - timedelta(minutes=minutes),
        )
    )
    db.commit()


def test_patrol_once_releases_stale_lock(db_session, factory):
    """哨兵 1：僵尸锁被回收 → 域重新可用。"""
    _make_stale_lock(db_session, task_id="stale-task-1", minutes=60)
    assert TL.get_lock_status(db_session).mining_domain["busy"] is True

    released = TL.patrol_once(session_factory=factory)

    assert "stale-task-1" in released, "僵尸锁未被 patrol 回收"
    assert TL.get_lock_status(db_session).mining_domain["busy"] is False


def test_patrol_once_keeps_fresh_lock(db_session, factory):
    """哨兵 2：心跳新鲜的锁不得被误杀（否则正在跑的实验被打断）。"""
    handle = TL.acquire_mining_lock(db_session, task_id="live-task", run_id="run-live")
    assert handle is not None

    released = TL.patrol_once(session_factory=factory)

    assert released == [], "patrol 误杀了心跳新鲜的锁"
    status = TL.get_lock_status(db_session)
    assert status.mining_domain["busy"] is True
    assert status.mining_domain["taskId"] == "live-task"


def test_patrol_once_preserves_write_queue(db_session, factory):
    """哨兵 3：回收 duckdb_write 时排队队列不得被一起删掉（T12 回归）。"""
    handle, pos = TL.acquire_write_slot(
        db_session, task_id="holder", run_id="run-holder"
    )
    assert handle is not None and pos == 0

    db_session.expunge_all()
    _, pos2 = TL.acquire_write_slot(db_session, task_id="waiter-1", run_id="run-w1")
    assert pos2 == 1
    db_session.expunge_all()
    _, pos3 = TL.acquire_write_slot(db_session, task_id="waiter-2", run_id="run-w2")
    assert pos3 == 2

    # 持有者心跳超时（模拟进程崩溃）
    row = db_session.get(TL.__dict__["TaskLock"], TL.LOCK_DUCKDB_WRITE)
    row.heartbeat_at = _utcnow() - timedelta(minutes=60)
    db_session.commit()

    TL.patrol_once(session_factory=factory)

    status = TL.get_lock_status(db_session)
    queue = status.duckdb_write.get("queue", [])
    assert "waiter-2" in queue, f"patrol 后排队队列丢失或不完整：{queue}"
    assert status.duckdb_write["taskId"] == "waiter-1", "队首未被拉起为新持有者"
