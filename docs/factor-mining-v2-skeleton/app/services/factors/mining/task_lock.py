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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.task_lock import TaskLock

# ══════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════

LOCK_MINING_DOMAIN = "mining_domain"
LOCK_DUCKDB_WRITE = "duckdb_write"

#: 心跳超时，秒（可配置；与需求 §3.1 / 向导 §8.2 的「默认 30 分钟」一致）
HEARTBEAT_TIMEOUT = 1800


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
    try:
        db.add(row)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(TaskLock, LOCK_MINING_DOMAIN)
        if existing is None:  # 竞态：刚好被释放，重试一次
            db.add(row)
            db.commit()
            return LockHandle(LOCK_MINING_DOMAIN, task_id, now)
        raise MiningDomainBusy(
            owner_task_id=existing.owner_task_id,
            owner_run_id=existing.owner_run_id,
        ) from None
    return LockHandle(LOCK_MINING_DOMAIN, task_id, now)


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

    # 被占 → 追加到队尾。用 SELECT ... FOR UPDATE 串行化队列写入，
    # 避免两个并发提交拿到同一位次。
    existing = db.execute(
        select(TaskLock).where(TaskLock.lock_key == LOCK_DUCKDB_WRITE).with_for_update()
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
    """
    row = db.get(TaskLock, lock_key)
    if row is None:
        return None
    if row.owner_task_id != task_id:
        # 非持有者不得释放（防止误释放他人锁）
        return None

    queue = _load_queue(row) if lock_key == LOCK_DUCKDB_WRITE else []
    db.delete(row)
    db.flush()

    next_task_id: str | None = None
    if trigger_queue and lock_key == LOCK_DUCKDB_WRITE and queue:
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

    与 `release_lock` 分离，便于在**独立事务**中执行——释放与提升分开提交，
    避免"锁已释放但队首提升失败"导致队列卡死。
    """
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
    """
    deadline = _utcnow() - timedelta(seconds=int(timeout_seconds))
    rows = db.execute(select(TaskLock).where(TaskLock.heartbeat_at < deadline)).scalars().all()

    released: list[str] = []
    for row in rows:
        released.append(row.owner_task_id)
        db.delete(row)
    if released:
        db.commit()
    return released


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
        else:
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
    "get_lock_status",
]
