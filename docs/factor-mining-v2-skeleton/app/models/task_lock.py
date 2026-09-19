"""双任务锁的表模型（设计文档 §5.2 / §6.5 / §7.1）。

现有代码**没有** DB 级锁表：`app/services/factors/store.py:31` 是进程内
`threading.RLock`，`app/services/factors/warehouse_locks.py` 是跨进程 `.lock` 文件。
本表提供**跨任务业务锁**，与上述两层叠加使用（业务锁 + 进程文件锁）。

原子性来源：`lock_key` 唯一约束 + 数据库事务。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskLock(Base):
    """业务锁。当前仅两个 lock_key：`mining_domain` / `duckdb_write`。

    - `mining_domain`：挖掘域唯一，判定 `(queued, running)`；冲突 → **直接拒绝**（不排队）
    - `duckdb_write`：所有写 DuckDB 的任务互斥；冲突 → **排队**（写 `queue_json`）
    """

    __tablename__ = "task_locks"

    #: 唯一约束是原子获取的关键——并发 INSERT 只有一个能成功
    lock_key: Mapped[str] = mapped_column(String(32), primary_key=True)

    owner_task_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    #: 心跳；超时（默认 1800s）由 expire_stale_locks 释放并通知
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    #: 排队者（仅 duckdb_write 使用）。形如
    #: [{"task_id": "...", "run_id": "...", "enqueued_at": "2026-09-16T10:00:00"}]
    queue_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TaskLock {self.lock_key} owner={self.owner_task_id}>"


__all__ = ["TaskLock"]
