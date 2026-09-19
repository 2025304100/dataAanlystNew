"""挖掘任务 runner：提交入口 + worker 生命周期 + 双心跳（SD-v2.0 §6.5，任务 T13）。

提交流程（需求 §3.1 提交流程 / §6.5）
==================================
    ① peek `mining_domain` → 忙则 **直接拒绝（不创建任务）**
       —— 对应错误契约 `MINING_DOMAIN_BUSY`：「本次提交被拒绝，未创建任务」
    ② `create_async_task(TASK_TYPE, payload)` —— 只写 DB **不启线程**
    ③ `acquire_mining_lock(task.id)` —— 竞态兜底（peek 后仍可能被抢）：
       失败则把刚建的任务标 failed 并抛错（响应仍是 409，任务记录留痕）
    ④ `acquire_write_slot(task.id)` → position（0=持有，>0=排队）
    ⑤ position == 0 → `_start_worker`；position > 0 → **不启动**，等待被拉起
    ⑥ 返回 `{task_id, run_id, status, queue_position, started}`

为什么排队任务不启动 worker（选项 B）
==================================
`duckdb_write` 的队列存在锁行的 `queue_json` 里。持有者释放时 `release_lock`
返回被拉起的 task_id，**由释放者的 finally 调 `_start_worker` 唤醒它**——
此时被拉起者已确认持有锁，不需要轮询/等待。
代价：若释放者进程在「释放」与「唤醒」之间崩溃，被拉起者会滞留 queued。
兜底是 `expire_stale_locks` + patrol（T14 字段校验任务补齐）；
本模块提供 `resume_pending_mining_tasks` 供运维/启动时手动补拉。

双心跳
======
async_tasks 的心跳超时是 5 分钟（`task_state_machine.HEARTBEAT_TIMEOUT`），
锁的心跳超时是 1800s（`task_lock.HEARTBEAT_TIMEOUT`）。worker 内起**一个**
守护泵按 `TASK_HEARTBEAT_SECONDS`（15s）同时刷新两者——一个泵刷两处，
避免两个线程节奏不一致。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from app.models.async_task import AsyncTaskRecord
from app.services import task_state_machine as TSM
from app.services.async_tasks import (
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    is_worker_stop_requested,
)
from app.services.factors.mining import task_lock as TL

logger = logging.getLogger(__name__)

#: 异步任务类型（`async_task_records.task_type`）
TASK_TYPE = "factor_mining"

#: worker 内部心跳泵间隔（秒）。同时覆盖两个超时：任务 5 分钟 / 锁 30 分钟。
TASK_HEARTBEAT_SECONDS = 15.0

StageRunner = Callable[[Any], dict[str, Any]]


# ══════════════════════════════════════════════════════════
# 结果与上下文
# ══════════════════════════════════════════════════════════


@dataclass
class MiningSubmitResult:
    task_id: str
    run_id: str
    status: str
    queue_position: int          # 0 = 已持有 duckdb_write；>0 = 排队位次
    started: bool                # worker 是否已启动（排队任务为 False）

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "status": self.status,
            "queue_position": self.queue_position,
            "started": self.started,
            "started_hint_zh": (
                "已启动" if self.started
                else f"duckdb_write 排队中（第 {self.queue_position} 位），"
                     "前序任务释放后自动开始"
            ),
        }


@dataclass
class MiningWorkerContext:
    """传给 `stage_runner` 的执行上下文。"""

    task_id: str
    run_id: str
    payload: dict[str, Any]
    holds_duckdb_write: bool

    def heartbeat(self) -> None:
        """阶段内长耗时步骤应周期性调用（§6.5：每代结束 + 每完成 10 个因子）。"""
        heartbeat_mining(self.task_id)

    def should_stop(self) -> bool:
        return is_worker_stop_requested(task_id=self.task_id)


# ══════════════════════════════════════════════════════════
# 提交
# ══════════════════════════════════════════════════════════


def submit_mining_run(
    payload: dict[str, Any], *,
    run_id: str,
    stage_runner: StageRunner | None = None,
    operator_id: str = "system",
) -> MiningSubmitResult:
    """提交一次挖掘任务（**同步部分**：建任务 + 抢双锁 + 启 worker）。

    Raises:
        MiningDomainBusy: 挖掘域已被占用（响应层转 409 `MINING_DOMAIN_BUSY`）。
            **保证未创建任务** —— peek 在 create 之前。
    """
    # ① peek：常见冲突路径在这里就拒绝，不留任务记录
    _reject_if_mining_busy()

    # ② 创建异步任务（只写 DB，不启线程 —— 任务卡点名的大坑）
    task = create_async_task(TASK_TYPE, dict(payload or {}), use_control_plane=True)

    # ③ mining_domain：peek 后仍可能被并发提交抢走 → 竞态兜底
    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        try:
            TL.acquire_mining_lock(db, task_id=task.id, run_id=run_id)
        except TL.MiningDomainBusy as exc:
            # 刚建的任务标 failed 留痕（审计可见），响应仍是 409
            _set_task(
                db, task.id,
                status="failed", stage="rejected",
                message=f"MINING_DOMAIN_BUSY: owner={exc.owner_task_id}",
                finished_at=_now(),
            )
            raise
        # ④ duckdb_write：空闲即持有（position=0），被占则排队
        _handle, position = TL.acquire_write_slot(db, task_id=task.id, run_id=run_id)
    finally:
        db.close()

    # ⑤ 只有持锁者启动 worker；排队者等持有者释放时被拉起
    started = position == 0
    if started:
        _start_worker(task.id, _make_worker(stage_runner))

    return MiningSubmitResult(
        task_id=task.id,
        run_id=run_id,
        status=task.status,
        queue_position=position,
        started=started,
    )


def _reject_if_mining_busy() -> None:
    """peek `mining_domain`；忙则抛 `MiningDomainBusy`（**不创建任务**）。"""
    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        status = TL.get_lock_status(db)
    finally:
        db.close()
    if status.mining_domain.get("busy"):
        raise TL.MiningDomainBusy(
            owner_task_id=str(status.mining_domain.get("taskId") or "unknown"),
            owner_run_id=status.mining_domain.get("runId"),
        )


# ══════════════════════════════════════════════════════════
# worker
# ══════════════════════════════════════════════════════════


def _make_worker(stage_runner: StageRunner | None) -> Callable[[str], None]:
    def _worker(task_id: str) -> None:
        run_mining_worker(task_id, stage_runner=stage_runner)
    return _worker


def run_mining_worker(
    task_id: str, *, stage_runner: StageRunner | None = None
) -> None:
    """挖掘 worker 主流程：状态迁移 → 双心跳泵 → 阶段执行 → 终态 → 释放双锁。

    `stage_runner(task_id, ctx) -> dict` 由调用方注入（真实 GA 在 T22/T23 接入）；
    缺省 `_default_stage_runner` 只走空生命周期，供基础设施联调。
    `stage_runner` 抛出的异常由 `_start_worker` 捕获并转 failed（既有语义，不改）。
    """
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status in ("cancelled", "done", "failed"):
            return
        payload: dict[str, Any] = {}
        if task.payload_json:
            import json

            try:
                payload = json.loads(task.payload_json)
            except ValueError:
                payload = {}
        run_id = str(payload.get("run_id") or "")

        # queued → running（走状态机校验，非法迁移会抛）
        TSM.transition(task_id, "running", db=db)
        _set_task(db, task_id, status="running", stage="running",
                  message="mining worker started", started_at=_now())

        # 双心跳泵：一个线程刷两处（任务 + 锁）
        heartbeat_stop, heartbeat_thread = _start_heartbeat_pump(task_id)

        # 确认自己确实持有 duckdb_write（防误唤醒：排队任务不该跑阶段）
        holds_write = _verify_holds_write(db, task_id)
        ctx = MiningWorkerContext(
            task_id=task_id, run_id=run_id, payload=payload,
            holds_duckdb_write=holds_write,
        )

        if ctx.should_stop():
            _set_task(db, task_id, status="cancelled", stage="cancelled",
                      message="cancelled before stages", finished_at=_now())
            return

        runner = stage_runner or _default_stage_runner
        result = runner(ctx) or {}

        if ctx.should_stop():
            _set_task(db, task_id, status="cancelled", stage="cancelled",
                      message="cancelled after stages", finished_at=_now())
            return

        _set_task(
            db, task_id,
            status="done", stage="done",
            percent=100,
            message=str(result.get("message_zh") or "mining finished"),
            result_json=_dumps(result),
            finished_at=_now(),
        )
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
        _release_all_locks(task_id)
        db.close()


def _default_stage_runner(ctx: MiningWorkerContext) -> dict[str, Any]:
    """M1a 基础设施骨架：占位阶段，验证生命周期（心跳/锁/终态）走通。

    真实 GA 循环在 T22/T23 接入时替换 `stage_runner` 注入；
    这里**绝不**抛 NotImplementedError —— 那会让每条提交都失败，
    基础设施就没法联调了。
    """
    ctx.heartbeat()
    return {"message_zh": "挖掘骨架完成（M1a 基础设施验证；GA 循环在 M1b 接入）"}


# ══════════════════════════════════════════════════════════
# 心跳 / 锁释放
# ══════════════════════════════════════════════════════════


def heartbeat_mining(task_id: str) -> None:
    """双心跳：async task（5 分钟超时）+ 锁（30 分钟超时）。"""
    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None and task.status not in ("done", "failed", "cancelled"):
            TSM.update_heartbeat(task_id, db=db)
        TL.heartbeat(db, lock_key=TL.LOCK_DUCKDB_WRITE, task_id=task_id)
        TL.heartbeat(db, lock_key=TL.LOCK_MINING_DOMAIN, task_id=task_id)
    finally:
        db.close()


def _start_heartbeat_pump(task_id: str) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _beat() -> None:
        while not stop_event.wait(TASK_HEARTBEAT_SECONDS):
            try:
                heartbeat_mining(task_id)
            except Exception:  # noqa: BLE001 - 心跳失败不应打断挖掘
                logger.warning("mining heartbeat failed for %s", task_id,
                               exc_info=True)

    thread = threading.Thread(target=_beat, name=f"mining-hb-{task_id[:8]}",
                              daemon=True)
    thread.start()
    return stop_event, thread


def _release_all_locks(task_id: str) -> str | None:
    """释放双锁；`duckdb_write` 拉起队首并**唤醒**它（启动其 worker）。

    Returns:
        被拉起的 task_id（供日志/测试断言）。
    """
    from app.db.session import get_session_local

    db = get_session_local()()
    next_task_id: str | None = None
    try:
        status = TL.get_lock_status(db)
        if status.duckdb_write.get("taskId") == task_id:
            next_task_id = TL.release_lock(
                db, lock_key=TL.LOCK_DUCKDB_WRITE, task_id=task_id
            )
        if status.mining_domain.get("taskId") == task_id:
            TL.release_lock(db, lock_key=TL.LOCK_MINING_DOMAIN, task_id=task_id)
    finally:
        db.close()
    if next_task_id:
        logger.info("mining task %s finished; promoted %s", task_id, next_task_id)
        _start_worker(next_task_id, _make_worker(None))
    return next_task_id


def _verify_holds_write(db: Any, task_id: str) -> bool:
    status = TL.get_lock_status(db)
    return status.duckdb_write.get("taskId") == task_id


def resume_pending_mining_tasks() -> list[str]:
    """补拉滞留在 queued 的挖掘任务（运维/应用启动时调用）。

    场景：持有者在「释放锁」与「唤醒队首」之间崩溃 → 队首任务滞留 queued。
    这里扫描 queued/running 的挖掘任务：若它持有 `duckdb_write` 但没有
    存活的 worker（无法直接判定，用「任务处于 queued 且锁行 owner 是它」
    近似），补启动 worker。
    """
    from app.db.session import get_session_local

    db = get_session_local()()
    resumed: list[str] = []
    try:
        rows = db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.task_type == TASK_TYPE,
                AsyncTaskRecord.status == "queued",
            )
        ).scalars().all()
        for task in rows:
            if _verify_holds_write(db, task.id):
                _start_worker(task.id, _make_worker(None))
                resumed.append(task.id)
    finally:
        db.close()
    return resumed


def _dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


from sqlalchemy import select  # noqa: E402  （resume_pending_mining_tasks 使用）


__all__ = [
    "TASK_TYPE",
    "TASK_HEARTBEAT_SECONDS",
    "MiningSubmitResult",
    "MiningWorkerContext",
    "submit_mining_run",
    "run_mining_worker",
    "heartbeat_mining",
    "resume_pending_mining_tasks",
]
