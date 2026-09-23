"""双任务锁实现（设计文档 §6.5 / §7.1，模块 M5）。

原子性策略
----------
获取锁 = 在**一个事务内** `INSERT` 一行到 `task_locks`。`lock_key` 是主键，
并发 `INSERT` 只有一个能提交成功 → 天然互斥，无需额外加锁，也无并发穿透。

- `mining_domain` 冲突 → `raise MiningDomainBusy`（**不排队**）
- `duckdb_write` 冲突 → 追加到 `queue_json`，返回排队位次（**排队**）

释放
----
`release_lock(..., trigger_queue=True)` 会删掉锁行并返回**被拉起的队首 task_id**，
由调用方（`task_runner`）去唤醒它。**不引入新调度器**。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.task_lock import TaskLock

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════

LOCK_MINING_DOMAIN = "mining_domain"
LOCK_DUCKDB_WRITE = "duckdb_write"

#: 心跳超时，秒（可配置；与需求 §3.1 / 向导 §8.2 的「默认 30 分钟」一致）
HEARTBEAT_TIMEOUT = 1800

#: `acquire_mining_lock` 在「锁刚好被释放」竞态下的最大重试次数
_ACQUIRE_MAX_RETRIES = 3

#: 队列读改写的**进程内**串行化锁。
#!
#! 为什么需要：排队路径是「读 queue_json → 追加 → 写回」三步，依赖
#! `SELECT ... FOR UPDATE` 串行化。**MySQL (InnoDB) 下正确**；但 SQLite
#! 方言会静默忽略 FOR UPDATE，两个并发事务可以都读到旧队列然后互相覆盖，
#! 导致排队任务静默丢失。加这把锁让单进程多线程（测试/单 worker 部署）
#! 也确定正确；多进程部署仍靠 MySQL 的行锁兜底。
_QUEUE_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════
# 异常与数据结构
# ══════════════════════════════════════════════════════════


class MiningDomainBusy(Exception):
    """`mining_domain` 已被占用 → 路由层转 409 MINING_DOMAIN_BUSY。

    attributes 供错误体拼装（需求要求带上"当前任务 ID/状态/代数"）。
    """

    def __init__(
        self,
        *,
        owner_task_id: str,
        owner_run_id: str | None = None,
        owner_status: str | None = None,
        owner_generation: int | None = None,
    ) -> None:
        super().__init__("another factor_mining task is already queued/running")
        self.owner_task_id = owner_task_id
        self.owner_run_id = owner_run_id
        self.owner_status = owner_status
        self.owner_generation = owner_generation


@dataclass
class LockHandle:
    lock_key: str
    owner_task_id: str
    acquired_at: datetime
    queue_position: int = 0  # 0 = 已持有；>0 = 排队位次（1-based）


@dataclass
class LockStatus:
    mining_domain: dict[str, Any] = field(default_factory=dict)
    duckdb_write: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"miningDomain": self.mining_domain, "duckdbWrite": self.duckdb_write}


# ══════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════


def _load_queue(row: TaskLock) -> list[dict[str, Any]]:
    if not row.queue_json:
        return []
    try:
        value = json.loads(row.queue_json)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _dump_queue(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


# ══════════════════════════════════════════════════════════
# 获取
# ══════════════════════════════════════════════════════════


def acquire_mining_lock(db: Session, *, task_id: str, run_id: str) -> LockHandle:
    """获取 `mining_domain`。冲突**直接拒绝**（不排队）。

    Raises:
        MiningDomainBusy: 已有 queued/running 的挖掘任务。
    """
    now = _utcnow()
    row = TaskLock(
        lock_key=LOCK_MINING_DOMAIN,
        owner_task_id=task_id,
        owner_run_id=run_id,
        acquired_at=now,
        heartbeat_at=now,
    )
    for _ in range(_ACQUIRE_MAX_RETRIES):
        try:
            db.add(row)
            db.commit()
            return LockHandle(LOCK_MINING_DOMAIN, task_id, now)
        except IntegrityError:
            db.rollback()
            existing = db.get(TaskLock, LOCK_MINING_DOMAIN)
            if existing is None:
                # 竞态：锁刚好在「冲突」与「回滚」之间被释放 → 重试
                continue
            raise MiningDomainBusy(
                owner_task_id=existing.owner_task_id,
                owner_run_id=existing.owner_run_id,
            ) from None
    # 极端情况：重试窗口内锁被反复抢放。此时绝不静默持锁 —— 明确失败，
    # 让调用方把它当「域忙碌」处理（失败是显式的，优于两个任务同时跑）。
    raise MiningDomainBusy(owner_task_id="unknown", owner_run_id=None)


def acquire_write_slot(
    db: Session, *, task_id: str, run_id: str
) -> tuple[LockHandle | None, int]:
    """获取 `duckdb_write`。空闲则立即持有；被占则**排队**。

    Returns:
        (handle, position)。position == 0 表示已持有；>0 表示排队位次（1-based）。
    """
    now = _utcnow()
    row = TaskLock(
        lock_key=LOCK_DUCKDB_WRITE,
        owner_task_id=task_id,
        owner_run_id=run_id,
        acquired_at=now,
        heartbeat_at=now,
    )
    try:
        db.add(row)
        db.commit()
        return LockHandle(LOCK_DUCKDB_WRITE, task_id, now, queue_position=0), 0
    except IntegrityError:
        db.rollback()

    # 被占 → 追加到队尾。读改写必须在 `_QUEUE_LOCK` 内完成：
    # MySQL 靠 SELECT FOR UPDATE 串行化，SQLite 忽略 FOR UPDATE，靠进程内锁兜底。
    with _QUEUE_LOCK:
        existing = db.execute(
            select(TaskLock).where(TaskLock.lock_key == LOCK_DUCKDB_WRITE)
            .with_for_update()
        ).scalar_one_or_none()
        if existing is None:
            # 锁刚被释放，重试一次
            return acquire_write_slot(db, task_id=task_id, run_id=run_id)

        queue = _load_queue(existing)
        if any(item.get("task_id") == task_id for item in queue):
            position = next(
                i + 1 for i, item in enumerate(queue) if item.get("task_id") == task_id
            )
        else:
            queue.append(
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "enqueued_at": now.isoformat(),
                }
            )
            existing.queue_json = _dump_queue(queue)
            position = len(queue)
        db.commit()
    return None, position


# ══════════════════════════════════════════════════════════
# 释放 / 拉起队首
# ══════════════════════════════════════════════════════════


def release_lock(
    db: Session, *, lock_key: str, task_id: str, trigger_queue: bool = True
) -> str | None:
    """释放锁；若 `duckdb_write` 且 `trigger_queue`，返回被拉起的队首 task_id。

    调用方拿到非 None 返回值后应负责唤醒该任务（复用 `async_tasks` 状态机）。

    实现顺序（同一事务内）：读队列 → 删锁行 → 用队首重建锁行。
    若重建失败整事务回滚，锁仍属于原持有者，不会出现"锁没了但队首没上来"。

    Raises:
        ValueError: `trigger_queue=False` —— 该组合会**静默丢弃整个排队队列**
            （队列存在锁行的 `queue_json` 里，删行即丢）。这是 T12 审查发现的
            缺陷；在没有人依赖该语义之前显式拒绝，优于静默丢任务。
    """
    if not trigger_queue:
        raise ValueError(
            "release_lock(trigger_queue=False) 会丢弃 duckdb_write 的整个排队队列"
            "（队列存在锁行 queue_json 里，删行即丢）。"
            "请使用默认的 trigger_queue=True；如需「只换持有者不释放」用 promote_queue_head。"
        )

    with _QUEUE_LOCK:
        row = db.get(TaskLock, lock_key)
        if row is None:
            return None
        if row.owner_task_id != task_id:
            # 非持有者不得释放（防止误释放他人锁）
            return None

        queue = _load_queue(row) if lock_key == LOCK_DUCKDB_WRITE else []
        # 用 Core 层删除：ORM 的 db.delete(row) + db.add(同主键新行) 会触发
        # SAWarning（同一 identity key 两个实例），Core delete 无此问题。
        db.execute(sa_delete(TaskLock).where(TaskLock.lock_key == lock_key))
        db.flush()
        db.expunge(row)   # 从 identity map 移除，避免 commit 时的 SAWarning

        next_task_id: str | None = None
        if lock_key == LOCK_DUCKDB_WRITE and queue:
            head = queue.pop(0)
            now = _utcnow()
            db.add(
                TaskLock(
                    lock_key=LOCK_DUCKDB_WRITE,
                    owner_task_id=head["task_id"],
                    owner_run_id=head.get("run_id"),
                    acquired_at=now,
                    heartbeat_at=now,
                    queue_json=_dump_queue(queue) if queue else None,
                )
            )
            next_task_id = head["task_id"]

        db.commit()
    return next_task_id


def promote_queue_head(db: Session, *, lock_key: str = LOCK_DUCKDB_WRITE) -> str | None:
    """把 `duckdb_write` 队列头部提升为持有者，返回其 task_id。

    ⚠️ 语义澄清（T12）：这是「**当前持有者主动让位**」—— 锁行不删，只换 owner。
    释放锁请用 `release_lock`（它在一个事务内完成删行+拉起，失败整体回滚，
    不存在"锁已释放但队首提升失败"）。两者不要混用。
    """
    with _QUEUE_LOCK:
        row = db.get(TaskLock, lock_key)
        if row is None:
            return None
        queue = _load_queue(row)
        if not queue:
            return None

        head = queue.pop(0)
        row.owner_task_id = head["task_id"]
        row.owner_run_id = head.get("run_id")
        row.acquired_at = _utcnow()
        row.heartbeat_at = _utcnow()
        row.queue_json = _dump_queue(queue) if queue else None
        db.commit()
    return head["task_id"]


# ══════════════════════════════════════════════════════════
# 心跳 / 超时
# ══════════════════════════════════════════════════════════


def heartbeat(db: Session, *, lock_key: str, task_id: str) -> None:
    """刷新心跳。更新点 = 每代结束 + 最终验证每完成 10 个因子（设计文档 §7.1）。"""
    row = db.get(TaskLock, lock_key)
    if row is not None and row.owner_task_id == task_id:
        row.heartbeat_at = _utcnow()
        db.commit()


def expire_stale_locks(
    db: Session, *, timeout_seconds: int = HEARTBEAT_TIMEOUT
) -> list[str]:
    """释放心跳超时的锁，返回被释放的 task_id 列表（供通知）。

    防死锁兜底：进程崩溃时锁行不会被清理，靠本函数回收。

    ⚠️ **对 `duckdb_write` 走与 `release_lock` 相同的「删行+拉起队首」路径**。
    T12 审查发现的缺陷：原实现直接 `DELETE` 锁行 —— 而排队队列存在锁行的
    `queue_json` 里，删行会把**整个排队队列一起删掉**，队列里的任务从此
    永远等不到锁（静默卡死）。修复后与正常释放语义一致。
    """
    deadline = _utcnow() - timedelta(seconds=int(timeout_seconds))
    rows = db.execute(
        select(TaskLock).where(TaskLock.heartbeat_at < deadline)
    ).scalars().all()

    released: list[str] = []
    for row in rows:
        released.append(row.owner_task_id)
        if row.lock_key == LOCK_DUCKDB_WRITE and _load_queue(row):
            # 有排队任务 → 复用 release_lock 的「删行+拉起」语义
            next_task_id = release_lock(
                db, lock_key=row.lock_key, task_id=row.owner_task_id
            )
            if next_task_id:
                released.append(next_task_id)  # 也通知被拉起的任务
        else:
            db.delete(row)
    if released:
        db.commit()
    return released


def patrol_once(
    session_factory: Any = None, *, timeout_seconds: int = HEARTBEAT_TIMEOUT
) -> list[str]:
    """巡检一次：回收心跳超时的锁（**供后台循环调用**）。

    ⚠️ 为什么要有这个函数：`expire_stale_locks()` 实现一直存在且语义正确，
    但**全仓没有任何调用方** —— 进程被强杀/重启后锁行永久残留，界面 Step4
    提交恒被拒（弹窗「已有挖掘任务进行中」），用户没有任何自救入口。
    本函数就是那个缺失的调用方入口：自带 session、失败不外抛（后台循环不能被
    一次异常打死），由 `app.main` 的 lifespan 起循环定期调用。

    Args:
        session_factory: 可调用返回 Session 的工厂；None 时用进程默认工厂
            （`DatabaseManager`），测试可注入以走真实代码路径。
        timeout_seconds: 心跳超时阈值，默认 `HEARTBEAT_TIMEOUT`（30 分钟）。

    Returns:
        被释放的 task_id 列表（含被拉起的队首）；异常时返回空列表。
    """
    if session_factory is None:
        from app.db.session import get_session_local  # 延迟导入，避免循环依赖

        session_factory = get_session_local()

    db = session_factory()
    try:
        return expire_stale_locks(db, timeout_seconds=timeout_seconds)
    except Exception:
        # 后台巡检：一次失败不得终止整个循环，也不得把异常抛给调用方
        logger.exception("mining lock patrol failed; will retry on next tick")
        try:
            db.rollback()
        except Exception:
            pass
        return []
    finally:
        db.close()


def get_lock_status(db: Session) -> LockStatus:
    """双锁总览（`GET /factor-mining/locks/status`）。"""
    status = LockStatus()
    for row in db.execute(select(TaskLock)).scalars().all():
        payload: dict[str, Any] = {
            "busy": True,
            "taskId": row.owner_task_id,
            "runId": row.owner_run_id,
            "acquiredAt": row.acquired_at.isoformat() if row.acquired_at else None,
            "heartbeatAt": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        }
        if row.lock_key == LOCK_MINING_DOMAIN:
            status.mining_domain = payload
        elif row.lock_key == LOCK_DUCKDB_WRITE:
            queue = _load_queue(row)
            payload["queue"] = [item.get("task_id") for item in queue]
            status.duckdb_write = payload

    if not status.mining_domain:
        status.mining_domain = {"busy": False}
    if not status.duckdb_write:
        status.duckdb_write = {"busy": False, "queue": []}
    return status


__all__ = [
    "LOCK_MINING_DOMAIN",
    "LOCK_DUCKDB_WRITE",
    "HEARTBEAT_TIMEOUT",
    "MiningDomainBusy",
    "LockHandle",
    "LockStatus",
    "acquire_mining_lock",
    "acquire_write_slot",
    "release_lock",
    "promote_queue_head",
    "heartbeat",
    "expire_stale_locks",
    "patrol_once",
    "get_lock_status",
]
