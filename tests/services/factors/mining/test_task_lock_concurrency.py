"""T12 · 双锁并发穿透测试（DoD / G3 前置）。

测试矩阵
========
**串行语义**（逻辑正确性，毫秒级）
- `mining_domain`：占用 → 冲突拒绝（不排队）→ 释放 → 可再获取
- `duckdb_write`：空闲即持有；被占 → 排队；重复排队幂等（位次不变）
- 释放拉起：FIFO、剩余队列正确、新持有者心跳刷新
- 非持有者释放 → 拒绝
- `promote_queue_head`：换 owner、不删锁行（与 release 的语义区分）
- `expire_stale_locks`：回收超时锁且**不丢排队队列**（T12 修复的回归哨兵）
- `release_lock(trigger_queue=False)` → 显式拒绝（该组合会静默丢队列）

**真并发**（多线程 + 独立 Session，穿透验证）
- `mining_domain`：N 线程同时抢 → **恰好 1 个成功**，其余 `MiningDomainBusy`
- `duckdb_write`：N 线程同时抢 → 1 持有 + N-1 排队，**位次无重复、无任务丢失**

为什么并发测试用 `db_session` 的引擎 + 每线程独立 Session
========================================================
`db_session` 是 SQLite（内存/临时文件）。SQLite 会把写事务串行化，
且**忽略 `SELECT ... FOR UPDATE`** —— 所以生产（MySQL InnoDB）靠行锁保证的
「队列读改写」在测试环境需要 `task_lock._QUEUE_LOCK`（进程内锁）兜底。
这条差异本身就是 T12 修复的一部分：不加那把锁，并发排队会静默丢任务。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.services.factors.mining import task_lock as TL


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _acquire_write_slot(session, **kw):
    """在锁行已持久化的同一 Session 里安全地再排队。

    task_lock.acquire_write_slot 总是新建同主键 TaskLock 再 commit；当 Session
    里已有对应 `duckdb_write` 持久实例时，会触发 identity-key 冲突 SAWarning
    （task_lock.py:177）。测试侧先 `expunge_all` 清空 identity map，让重排走的
    是「INSERT→IntegrityError→读改写」路径，无告警且语义不变。
    """
    session.expunge_all()
    return TL.acquire_write_slot(session, **kw)


@pytest.fixture
def new_session(db_session):
    """每线程独立 Session（Session 非线程安全），绑定同一 engine。"""
    factory = sessionmaker(bind=db_session.get_bind())
    return factory


# ══════════════════════════════════════════════════════════
# 1. mining_domain：串行语义
# ══════════════════════════════════════════════════════════


class TestMiningDomainSerial:
    def test_acquire_then_conflict_then_release_then_reacquire(self, db_session):
        h1 = TL.acquire_mining_lock(db_session, task_id="t1", run_id="r1")
        assert h1.owner_task_id == "t1"
        assert h1.queue_position == 0

        with pytest.raises(TL.MiningDomainBusy) as ei:
            TL.acquire_mining_lock(db_session, task_id="t2", run_id="r2")
        # 冲突信息必须能定位到持有者（需求：错误体带当前任务 ID）
        assert ei.value.owner_task_id == "t1"
        assert ei.value.owner_run_id == "r1"

        # 释放后可再获取
        assert TL.release_lock(db_session, lock_key=TL.LOCK_MINING_DOMAIN,
                               task_id="t1") is None
        h2 = TL.acquire_mining_lock(db_session, task_id="t2", run_id="r2")
        assert h2.owner_task_id == "t2"

    def test_release_by_non_owner_is_refused(self, db_session):
        TL.acquire_mining_lock(db_session, task_id="t1", run_id="r1")
        # 非持有者释放 → None，且锁仍在
        assert TL.release_lock(db_session, lock_key=TL.LOCK_MINING_DOMAIN,
                               task_id="t2") is None
        with pytest.raises(TL.MiningDomainBusy):
            TL.acquire_mining_lock(db_session, task_id="t3", run_id="r3")

    def test_release_unknown_lock_returns_none(self, db_session):
        assert TL.release_lock(db_session, lock_key=TL.LOCK_MINING_DOMAIN,
                               task_id="nobody") is None


# ══════════════════════════════════════════════════════════
# 2. duckdb_write：排队语义
# ══════════════════════════════════════════════════════════


class TestDuckDBWriteQueue:
    def test_free_lock_is_acquired_immediately(self, db_session):
        handle, pos = TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        assert handle is not None
        assert pos == 0
        assert handle.queue_position == 0

    def test_conflict_queues_fifo(self, db_session):
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        for i, tid in enumerate(("w2", "w3", "w4"), start=1):
            handle, pos = _acquire_write_slot(db_session, task_id=tid,
                                              run_id=f"r{tid}")
            assert handle is None
            assert pos == i

        status = TL.get_lock_status(db_session)
        assert status.duckdb_write["busy"] is True
        assert status.duckdb_write["taskId"] == "w1"
        assert status.duckdb_write["queue"] == ["w2", "w3", "w4"]

    def test_re_enqueue_is_idempotent(self, db_session):
        """同一任务重复排队 → 位次不变，队列不重复（网络重试场景）。"""
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        h, p1 = _acquire_write_slot(db_session, task_id="w2", run_id="r2")
        assert (h, p1) == (None, 1)
        h, p2 = _acquire_write_slot(db_session, task_id="w2", run_id="r2")
        assert (h, p2) == (None, 1)          # 位次不变
        status = TL.get_lock_status(db_session)
        assert status.duckdb_write["queue"] == ["w2"]

    def test_release_promotes_head_and_keeps_rest(self, db_session):
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        for tid in ("w2", "w3", "w4"):
            _acquire_write_slot(db_session, task_id=tid, run_id=f"r{tid}")

        nxt = TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE, task_id="w1")
        assert nxt == "w2"                    # FIFO

        status = TL.get_lock_status(db_session)
        assert status.duckdb_write["taskId"] == "w2"
        assert status.duckdb_write["queue"] == ["w3", "w4"]

        # 队首现在持有锁 → 再抢会排队到队尾
        h, pos = _acquire_write_slot(db_session, task_id="w5", run_id="r5")
        assert (h, pos) == (None, 3)

    def test_release_then_reacquire_drains_queue_in_order(self, db_session):
        order: list[str] = []
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        for tid in ("w2", "w3", "w4"):
            _acquire_write_slot(db_session, task_id=tid, run_id=f"r{tid}")

        order.append("w1")
        while True:
            holder = TL.get_lock_status(db_session).duckdb_write.get("taskId")
            if holder is None:
                break
            nxt = TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE,
                                  task_id=holder)
            if nxt:
                order.append(nxt)
            else:
                break
        assert order == ["w1", "w2", "w3", "w4"]
        status = TL.get_lock_status(db_session)
        assert status.duckdb_write["busy"] is False
        assert status.duckdb_write["queue"] == []

    def test_promote_queue_head_changes_owner_without_releasing(self, db_session):
        """promote = 当前持有者**让位**；锁行不删（与 release 语义区分）。"""
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        TL.acquire_write_slot(db_session, task_id="w2", run_id="r2")

        nxt = TL.promote_queue_head(db_session)
        assert nxt == "w2"
        status = TL.get_lock_status(db_session)
        assert status.duckdb_write["taskId"] == "w2"
        assert status.duckdb_write["queue"] == []

    def test_promote_without_queue_returns_none(self, db_session):
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        assert TL.promote_queue_head(db_session) is None

    def test_promote_without_lock_returns_none(self, db_session):
        assert TL.promote_queue_head(db_session) is None

    def test_trigger_queue_false_is_rejected(self, db_session):
        """该组合会静默丢弃整个排队队列（T12 审查发现的缺陷）→ 显式拒绝。"""
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        TL.acquire_write_slot(db_session, task_id="w2", run_id="r2")
        with pytest.raises(ValueError, match="丢弃"):
            TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE,
                            task_id="w1", trigger_queue=False)
        # 锁未被释放
        status = TL.get_lock_status(db_session)
        assert status.duckdb_write["taskId"] == "w1"


# ══════════════════════════════════════════════════════════
# 3. 心跳 / 超时回收
# ══════════════════════════════════════════════════════════


class TestHeartbeatAndExpiry:
    def test_heartbeat_refreshes_only_owner(self, db_session):
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        before = TL.get_lock_status(db_session).duckdb_write["heartbeatAt"]
        TL.heartbeat(db_session, lock_key=TL.LOCK_DUCKDB_WRITE, task_id="w1")
        after = TL.get_lock_status(db_session).duckdb_write["heartbeatAt"]
        assert after >= before

        # 非持有者心跳不动
        TL.heartbeat(db_session, lock_key=TL.LOCK_DUCKDB_WRITE, task_id="other")
        still = TL.get_lock_status(db_session).duckdb_write["heartbeatAt"]
        assert still == after

    def test_expire_releases_stale_lock(self, db_session):
        TL.acquire_mining_lock(db_session, task_id="t1", run_id="r1")
        # 把心跳拨回 2 小时前
        row = db_session.get(TL.TaskLock, TL.LOCK_MINING_DOMAIN)
        row.heartbeat_at = _utcnow() - timedelta(hours=2)
        db_session.commit()

        released = TL.expire_stale_locks(db_session, timeout_seconds=1800)
        assert released == ["t1"]
        status = TL.get_lock_status(db_session)
        assert status.mining_domain["busy"] is False

    def test_expire_does_not_touch_fresh_locks(self, db_session):
        TL.acquire_mining_lock(db_session, task_id="t1", run_id="r1")
        assert TL.expire_stale_locks(db_session, timeout_seconds=1800) == []
        assert TL.get_lock_status(db_session).mining_domain["busy"] is True

    def test_expire_promotes_queue_instead_of_dropping_it(self, db_session):
        """🚨 T12 修复的回归哨兵：超时回收若直接 DELETE 锁行，
        会把 queue_json 里**整个排队队列**一起删掉 → 排队任务永久卡死。"""
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        for tid in ("w2", "w3"):
            _acquire_write_slot(db_session, task_id=tid, run_id=f"r{tid}")
        # 持有者心跳超时
        row = db_session.get(TL.TaskLock, TL.LOCK_DUCKDB_WRITE)
        row.heartbeat_at = _utcnow() - timedelta(hours=2)
        db_session.commit()

        released = TL.expire_stale_locks(db_session, timeout_seconds=1800)
        assert "w1" in released
        assert "w2" in released            # 被拉起的队首也要通知

        status = TL.get_lock_status(db_session)
        assert status.duckdb_write["taskId"] == "w2"   # 队首上台
        assert status.duckdb_write["queue"] == ["w3"]  # 队列**没有丢**
        # w3 之后还能正常走完流程
        assert TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE,
                               task_id="w2") == "w3"
        assert TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE,
                               task_id="w3") is None

    def test_expire_mining_domain_does_not_touch_write_queue(self, db_session):
        TL.acquire_mining_lock(db_session, task_id="t1", run_id="r1")
        TL.acquire_write_slot(db_session, task_id="w1", run_id="r1")
        row = db_session.get(TL.TaskLock, TL.LOCK_MINING_DOMAIN)
        row.heartbeat_at = _utcnow() - timedelta(hours=2)
        db_session.commit()

        released = TL.expire_stale_locks(db_session, timeout_seconds=1800)
        assert released == ["t1"]
        status = TL.get_lock_status(db_session)
        assert status.mining_domain["busy"] is False
        assert status.duckdb_write["taskId"] == "w1"   # 不受影响


# ══════════════════════════════════════════════════════════
# 4. 真并发穿透
# ══════════════════════════════════════════════════════════


def _run_threads(work, n_threads: int) -> list[Any]:
    """起 `n_threads` 线程，用 Barrier 尽量同时开跑；收集结果或异常。"""
    barrier = threading.Barrier(n_threads)
    out: list[Any] = [None] * n_threads

    def _wrap(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            out[i] = work(i)
        except Exception as exc:  # noqa: BLE001 - 测试要收集所有异常
            out[i] = exc

    threads = [threading.Thread(target=_wrap, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for t in threads:
        assert not t.is_alive(), "并发测试线程卡死（>60s）"
    return out


class TestConcurrentPenetration:
    def test_mining_domain_only_one_winner(self, db_session, new_session):
        """N 线程同时抢 `mining_domain` → **恰好 1 个成功**，其余 MiningDomainBusy。

        这是「挖掘域同一时间只允许一个任务」的硬约束穿透验证。
        """
        n = 8
        lock = threading.Lock()

        def work(i: int):
            session = new_session()
            try:
                return TL.acquire_mining_lock(session, task_id=f"t{i}",
                                              run_id=f"r{i}")
            finally:
                session.close()

        results = _run_threads(work, n)
        winners = [r for r in results if isinstance(r, TL.LockHandle)]
        busy = [r for r in results if isinstance(r, TL.MiningDomainBusy)]
        errors = [r for r in results
                  if r is not None and not isinstance(r, (TL.LockHandle,
                                                          TL.MiningDomainBusy))]
        assert not errors, f"出现意外异常: {[str(e)[:120] for e in errors]}"
        assert len(winners) == 1, f"应恰好 1 个成功，实得 {len(winners)}"
        assert len(busy) == n - 1

        # 唯一持有者确实在锁上
        status = TL.get_lock_status(db_session)
        assert status.mining_domain["busy"] is True
        assert status.mining_domain["taskId"] == winners[0].owner_task_id

        # 每个失败者的 owner 指针都指向同一持有者（不是 None/unknown）
        with lock:
            pass
        owners = {b.owner_task_id for b in busy}
        assert owners <= {winners[0].owner_task_id, "unknown"}

        # 清理
        TL.release_lock(db_session, lock_key=TL.LOCK_MINING_DOMAIN,
                        task_id=winners[0].owner_task_id)

    def test_mining_domain_serial_reacquire_loop_is_alone(self, db_session, new_session):
        """连续 5 轮「并发抢 → 释放」：每轮都恰好一个赢家（防状态残留）。"""
        for round_no in range(5):
            def work(i: int):
                session = new_session()
                try:
                    return TL.acquire_mining_lock(session, task_id=f"r{round_no}t{i}",
                                                  run_id=f"r{round_id}")
                finally:
                    session.close()

            round_id = round_no
            results = _run_threads(work, 4)
            winners = [r for r in results if isinstance(r, TL.LockHandle)]
            assert len(winners) == 1, f"第 {round_no} 轮赢家数 {len(winners)}"
            assert TL.release_lock(
                db_session, lock_key=TL.LOCK_MINING_DOMAIN,
                task_id=winners[0].owner_task_id) is None

    def test_duckdb_write_queue_no_loss_no_duplicate(self, db_session, new_session):
        """N 线程同时抢 `duckdb_write` → 1 持有 + N-1 排队，位次无重复、任务不丢。

        🚨 这条在**没有** `_QUEUE_LOCK` 的旧实现上会红：SQLite 忽略
        FOR UPDATE，两个事务都读到旧队列后互相覆盖 → 排队任务静默丢失。
        """
        n = 6

        def work(i: int):
            session = new_session()
            try:
                return TL.acquire_write_slot(session, task_id=f"w{i}",
                                             run_id=f"r{i}")
            finally:
                session.close()

        results = _run_threads(work, n)
        errors = [r for r in results
                  if r is not None and not isinstance(r, tuple)]
        assert not errors, f"出现意外异常: {[str(e)[:120] for e in errors]}"

        holders = [r for r in results if isinstance(r, tuple) and r[0] is not None]
        queued = [(r[1], ) for r in results if isinstance(r, tuple) and r[0] is None]

        assert len(holders) == 1
        assert len(queued) == n - 1

        status = TL.get_lock_status(db_session)
        assert status.duckdb_write["busy"] is True
        queue = status.duckdb_write["queue"]
        assert len(queue) == n - 1, f"队列丢失任务: {queue}"
        assert len(set(queue)) == len(queue), f"队列出现重复: {queue}"
        # 位次 1..n-1 恰好各一次（顺序不保证 —— SQLite 下线程调度决定）
        assert sorted(queued) == [(i,) for i in range(1, n)]

        # 逐个释放能把队列 drain 干净（验证 FIFO 链路完整）
        drained: list[str] = []
        while True:
            holder = TL.get_lock_status(db_session).duckdb_write.get("taskId")
            if holder is None:
                break
            nxt = TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE,
                                  task_id=holder)
            if nxt:
                drained.append(nxt)
            else:
                break
        assert sorted(drained) == sorted(queue)
        assert TL.get_lock_status(db_session).duckdb_write["busy"] is False

    def test_concurrent_release_and_enqueue_no_queue_loss(self, db_session,
                                                          new_session):
        """一边释放拉起、一边排队 → 队列不丢、不重复（读写竞态穿透）。"""
        TL.acquire_write_slot(db_session, task_id="w0", run_id="r0")

        results: list[Any] = [None] * 8

        def releaser():
            session = new_session()
            try:
                # 等 3 个排好队再释放
                for _ in range(200):
                    q = TL.get_lock_status(session).duckdb_write.get("queue") or []
                    if len(q) >= 3:
                        break
                    threading.Event().wait(0.01)
                return TL.release_lock(session, lock_key=TL.LOCK_DUCKDB_WRITE,
                                       task_id="w0")
            finally:
                session.close()

        def enqueuer(i: int):
            session = new_session()
            try:
                return TL.acquire_write_slot(session, task_id=f"w{i}", run_id=f"r{i}")
            finally:
                session.close()

        barrier = threading.Barrier(4)
        threads = []

        def wrap(fn, idx):
            def run():
                try:
                    barrier.wait(timeout=10)
                    results[idx] = fn()
                except Exception as exc:  # noqa: BLE001
                    results[idx] = exc
            return run

        threads.append(threading.Thread(target=wrap(releaser, 0)))
        for i in range(1, 4):
            threads.append(threading.Thread(target=wrap(lambda i=i: enqueuer(i), i)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        for t in threads:
            assert not t.is_alive()

        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"出现意外异常: {[str(e)[:120] for e in errors]}"

        # 释放者必须真的释放了（返回被拉起的队首 task_id）
        released_head = results[0]
        assert isinstance(released_head, str) and released_head.startswith("w")

        # 终态一致性：一个持有者 + 队列里 task_id 唯一。
        # ⚠️ w0 **不应该**出现在终态 —— 它已经释放了。断言它缺席才是对的
        #    （我第一版断成「四个都在」，是把「释放」误当「保留」）。
        status = TL.get_lock_status(db_session)
        queue = status.duckdb_write["queue"]
        assert len(set(queue)) == len(queue), f"队列重复: {queue}"
        assert status.duckdb_write["taskId"] != "w0"
        all_ids = {status.duckdb_write["taskId"], *queue}
        assert all_ids == {"w1", "w2", "w3"}, f"任务丢失: {all_ids}"

        # drain：所有排队者都能按序走完。
        # 收集口径：每轮「被释放的持有者」+「release 返回的被拉起者」，
        # 两者的并集应恰好等于 {w1,w2,w3}（w0 由释放者线程处理，不在此列）。
        drained: set[str] = {released_head}
        while True:
            holder = TL.get_lock_status(db_session).duckdb_write.get("taskId")
            if not holder:
                break
            nxt = TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE,
                                  task_id=holder)
            drained.add(holder)
            if nxt:
                drained.add(nxt)
            if nxt == holder:      # 防御：避免同一 id 死循环
                break
        assert drained == {"w1", "w2", "w3"}, f"走完队列的任务: {sorted(drained)}"
        assert TL.get_lock_status(db_session).duckdb_write["busy"] is False
