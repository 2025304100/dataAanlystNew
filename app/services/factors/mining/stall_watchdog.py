"""停滞 run 看门狗：按**任务推进**回收卡死的挖掘批次（2026-09-23）。

为什么需要它（与 `task_lock.expire_stale_locks` 的分工）
======================================================
- `expire_stale_locks` 按**心跳**判定：只救「进程崩溃」——心跳停了才会过期。
- 但实测到的卡死形态是：**worker 线程卡在初代评估，心跳泵仍在正常刷新** →
  `heartbeat_at` 永远新鲜，锁永远不超时，`POST /runs` 永远 409。
  真实现场：

      locks: miningDomain.busy=true, runId=f1cbd75d…, heartbeatAt=01:31:56（当前 01:31:58）
      runs:  f1cbd75d 已被用户 discard，但 worker 未停、锁未放

  用户此时**没有任何自救手段**（批次列表里那个 running 也点不出停止效果）。

因此本模块按**推进时间**判定：`status='running'` 且 `updated_at` 长时间不动 →
置 `failed` + `error_code='STALLED'` + 释放它占的双锁，让功能入口恢复可用。

接入方式：由 `app.main._mining_lock_patrol_loop`（60s）与锁巡检同一个 tick 调用。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor_mining import FactorMiningRun
from app.models.task_lock import TaskLock
from app.services.factors.mining import task_lock as TL

logger = logging.getLogger(__name__)

#: 超过这么久没有代数推进（`updated_at` 不动）即判为停滞。
#: 取值权衡：单代评估实测可达数分钟，10 分钟能让正常慢任务不被误杀，
#: 又不至于让功能入口被锁死太久。
STALL_THRESHOLD_SECONDS = 600

#: 停滞批次的错误码（前端按它给用户可读文案，见 i18n `miningRunErrorStalled`）
ERROR_CODE_STALLED = "STALLED"

#: 终态：这些状态下的批次**不该再持有任何锁**
_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "converged", "invalidated"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def find_stalled_runs(
    db: Session, *, threshold_seconds: int = STALL_THRESHOLD_SECONDS
) -> list[str]:
    """**只报告不修改**：返回长时间无推进的 running 批次 id（供上层预览/报警）。"""
    deadline = _utcnow() - timedelta(seconds=int(threshold_seconds))
    rows = db.execute(
        select(FactorMiningRun).where(FactorMiningRun.status == "running")
    ).scalars().all()
    stalled: list[str] = []
    for row in rows:
        last_progress = row.updated_at or row.created_at
        if last_progress is None or last_progress < deadline:
            stalled.append(row.id)
    return stalled


def _request_worker_stop(task_id: str | None) -> None:
    """让停滞 run 的 worker 尽快自止（DEF-12）。

    看门狗过去只改库+放锁，**不停 worker**——run 已 failed 后评估线程仍在
    跑，内存持续攀升（实测 2.3GB→4.3GB）。这里对仍处非终态的任务写
    `cancel_requested=1`；worker 的 `should_stop()`（含 5s 节流的取消检查，
    见 task_runner）会在下一个检查点退出。任务已是终态时 cancel API 自身
    幂等短路；任务记录不存在（如单测注入）则跳过。
    """
    if not task_id:
        return
    try:
        from app.db.session import get_session_local
        from app.models.async_task import AsyncTaskRecord
        from app.services.async_tasks import cancel_async_task

        db = get_session_local()()
        try:
            row = db.get(AsyncTaskRecord, task_id)
        finally:
            db.close()
        if row is None:
            logger.info(
                "Stall watchdog: task %s not found; skip cancel request",
                task_id)
            return
        cancel_async_task(task_id)
        logger.warning(
            "Stall watchdog: cancel requested for worker task %s", task_id)
    except Exception:  # noqa: BLE001 - 请求停止失败不影响放锁主流程
        logger.exception(
            "Stall watchdog: failed to request stop for task %s", task_id)


def reap_stalled_runs(
    db: Session, *, threshold_seconds: int = STALL_THRESHOLD_SECONDS
) -> list[str]:
    """把停滞的 running 批次置 `failed` 并**释放它占的锁**，返回被处理的 run_id。

    安全性要点：
    - 只处理 `status == 'running'`（终态幂等跳过，不重写历史）；
    - 只释放 `owner_run_id` 恰好等于该 run 的锁 —— 绝不误放他人/其他批次的锁；
    - 释放走 `task_lock.release_lock`（同一事务语义，duckdb_write 有队列时拉起队首）；
    - 放锁后对 worker 任务请求取消（DEF-12：不停 worker 的 reap = 僵尸评估线程）。
    """
    run_ids = find_stalled_runs(db, threshold_seconds=threshold_seconds)
    if not run_ids:
        return []

    reaped: list[str] = []
    owner_task_ids: set[str] = set()
    for run_id in run_ids:
        row = db.get(FactorMiningRun, run_id)
        if row is None or row.status != "running":
            continue  # 竞态：期间已被别的路径处理
        row.status = "failed"
        row.error_code = ERROR_CODE_STALLED
        db.commit()
        logger.warning(
            "Stall watchdog: run %s marked failed (%s, no progress for %ss)",
            run_id, ERROR_CODE_STALLED, threshold_seconds,
        )

        # 释放该 run 占的双锁（心跳可能仍然新鲜，不能等过期）
        for lock_key in (TL.LOCK_MINING_DOMAIN, TL.LOCK_DUCKDB_WRITE):
            lock = db.get(TaskLock, lock_key)
            if lock is None or lock.owner_run_id != run_id:
                continue
            owner = lock.owner_task_id
            if owner:
                owner_task_ids.add(str(owner))
            TL.release_lock(db, lock_key=lock_key, task_id=owner)
            logger.warning(
                "Stall watchdog: released %s held by stalled run %s (task %s)",
                lock_key, run_id, owner,
            )
        reaped.append(run_id)

    # DEF-12：对仍存活的任务请求取消，让 worker 在下一个检查点自止
    # （不 join/kill 线程——协作式退出，worker finally 会做幂等清理）。
    for task_id in owner_task_ids:
        _request_worker_stop(task_id)
    return reaped


def release_orphan_locks(db: Session) -> list[str]:
    """释放「**终态批次 / 不存在的批次**」仍持有的锁，返回涉及的 run_id 列表。

    为什么必须有这一条（实测现场）：
    用户 discard 掉一个批次（run → cancelled）后，**worker 线程并不会因此停止**，
    它（以及心跳泵）继续持有 `mining_domain` / `duckdb_write` —— 于是：
    - `expire_stale_locks` 不回收（心跳还新鲜）；
    - `reap_stalled_runs` 也不处理（它只看 `running`）。
    结果 `POST /runs` 永久 409，功能入口彻底关闭。

    判据是「锁的存在是否还有正当性」：批次已终态（或根本不存在）⇒ 锁是孤儿。
    """
    released: list[str] = []
    rows = db.execute(select(TaskLock)).scalars().all()
    for lock in rows:
        run_id = lock.owner_run_id
        if not run_id:
            continue
        run = db.get(FactorMiningRun, run_id)
        status = str(getattr(run, "status", "") or "")
        if run is not None and status not in _TERMINAL_STATUSES:
            continue  # running/draft 等仍属正当持有
        owner = lock.owner_task_id
        reason = "批次已终态" if run is not None else "批次不存在"
        TL.release_lock(db, lock_key=lock.lock_key, task_id=owner)
        logger.warning(
            "Orphan lock released: %s held by %s (%s, status=%s)",
            lock.lock_key, run_id, reason, status or "-",
        )
        released.append(run_id)
    return released


def reap_on_boot(db: Session) -> list[str]:
    """**进程启动时**清理上一次进程遗留的 running 批次（标 STALLED + 释放锁）。

    为什么必须有这一条（实测现场 2026-09-23）：
    进程重启后锁表仍留着上次 running 批次的锁（心跳已停止），而
    - `release_orphan_locks` 只管**终态**批次，running 不碰；
    - `reap_stalled_runs` 要等 `STALL_THRESHOLD_SECONDS`(600s) 才判停滞；
    - `expire_stale_locks` 按心跳超时要等 1800s。
    ⇒ **用户重启后端后 10~30 分钟内提交一律 409（「已有挖掘任务进行中」）**，
      而界面上的那个 running 批次其实早已随进程消失。

    安全性前提：本项目为**单进程部署**（`uvicorn` 单实例，见 `scripts/dev_services.py`），
    因此进程启动时**不可能存在其它进程的活 worker**，遗留 running 必为死任务。
    若将来改为多 worker/多实例部署，本函数需退化为「按心跳超时」的保守判定。

    Returns:
        被处理的 run_id 列表（幂等：重复调用第二次返回空）。
    """
    rows = db.execute(
        select(FactorMiningRun).where(FactorMiningRun.status == "running")
    ).scalars().all()
    if not rows:
        return []

    reaped: list[str] = []
    for row in rows:
        row.status = "failed"
        row.error_code = ERROR_CODE_STALLED
        db.commit()
        logger.warning(
            "Boot cleanup: run %s marked failed (%s) — 进程重启，原 worker 已不存在",
            row.id, ERROR_CODE_STALLED,
        )
        for lock_key in (TL.LOCK_MINING_DOMAIN, TL.LOCK_DUCKDB_WRITE):
            lock = db.get(TaskLock, lock_key)
            if lock is None or lock.owner_run_id != row.id:
                continue
            TL.release_lock(db, lock_key=lock_key, task_id=lock.owner_task_id)
            logger.warning("Boot cleanup: released %s held by %s", lock_key, row.id)
        reaped.append(row.id)
    return reaped


def watchdog_once(
    session_factory: Any = None, *, threshold_seconds: int = STALL_THRESHOLD_SECONDS
) -> list[str]:
    """巡检一次（自带 session、异常不外抛）—— 供后台循环调用。

    顺序：先清孤儿锁（终态/不存在的批次），再把停滞的 running 置 failed 并放锁。
    """
    if session_factory is None:
        from app.db.session import get_session_local  # 延迟导入，避免循环依赖

        session_factory = get_session_local()

    db = session_factory()
    try:
        release_orphan_locks(db)
        return reap_stalled_runs(db, threshold_seconds=threshold_seconds)
    except Exception:
        logger.exception("stall watchdog failed; will retry on next tick")
        try:
            db.rollback()
        except Exception:
            pass
        return []
    finally:
        db.close()


__all__ = [
    "STALL_THRESHOLD_SECONDS",
    "ERROR_CODE_STALLED",
    "find_stalled_runs",
    "reap_stalled_runs",
    "release_orphan_locks",
    "reap_on_boot",
    "watchdog_once",
]
