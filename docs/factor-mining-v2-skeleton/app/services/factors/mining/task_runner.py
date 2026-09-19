"""挖掘域异步任务唯一入口（设计文档 §6.5，模块 M5）。

⚠️ 关键实现约束（设计文档 §2.1 第 14 条 / V8）：
   `async_tasks.create_async_task(task_type, payload, *, use_control_plane, force_new)`
   **只创建 DB 记录，不启动线程**。返回后必须显式调用
   `async_tasks._start_worker(task_id, worker_func)`，否则任务永远停在 `queued`。

任务类型（设计文档 §3.2）：
   factor_mining             持 mining_domain + duckdb_write
   factor_mining_validation  独立类型，**不持 mining_domain**
   data_mirror               持 duckdb_write
   factor_grade_review       scheduled_tasks 触发（M2）
"""
from __future__ import annotations

from typing import Any, Callable

from app.db.session import get_session_local
from app.services import async_tasks
from app.services.factors.mining import task_lock as mining_lock
from app.services.task_state_machine import update_heartbeat, update_progress

TASK_TYPE_MINING = "factor_mining"
TASK_TYPE_VALIDATION = "factor_mining_validation"
TASK_TYPE_MIRROR = "data_mirror"
TASK_TYPE_GRADE_REVIEW = "factor_grade_review"


# ══════════════════════════════════════════════════════════
# 创建 + 启动（唯一入口）
# ══════════════════════════════════════════════════════════


def _create_and_start(
    *,
    task_type: str,
    payload: dict[str, Any],
    worker: Callable[[str], None],
) -> str:
    """创建异步任务并**立即启动 worker**。返回 task_id。"""
    task = async_tasks.create_async_task(task_type, payload, force_new=True)
    # create_async_task 只写 DB —— 必须显式启动
    async_tasks._start_worker(task.id, worker)
    return task.id


def submit_mining_task(*, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """提交挖掘任务：先双锁检查，再创建任务。

    Returns:
        {"task_id": str, "queue_position": int}
    Raises:
        MiningDomainBusy: 路由层转 409 MINING_DOMAIN_BUSY。
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task_id = f"mining-{run_id}"
        # ① mining_domain：冲突直接拒绝（不排队）
        mining_lock.acquire_mining_lock(db, task_id=task_id, run_id=run_id)
        # ② duckdb_write：空闲即持有；被占则排队
        handle, position = mining_lock.acquire_write_slot(
            db, task_id=task_id, run_id=run_id
        )
    finally:
        db.close()

    if position > 0:
        # 排队中：只记录任务，不启动 worker（等前面释放时由 release_lock 拉起）
        task = async_tasks.create_async_task(
            TASK_TYPE_MINING, {"run_id": run_id, **payload}, force_new=True
        )
        return {"task_id": task.id, "queue_position": position}

    started = _create_and_start(
        task_type=TASK_TYPE_MINING,
        payload={"run_id": run_id, **payload},
        worker=run_mining_task,
    )
    return {"task_id": started, "queue_position": 0}


# ══════════════════════════════════════════════════════════
# Worker 入口
# ══════════════════════════════════════════════════════════


def run_mining_task(task_id: str) -> None:
    """挖掘主任务。

    阶段（设计文档 §4.3）：
        P0 数据准备 → P1 初始种群 → P2 逐代进化 → P3 最终验证
        → P4 结果与沉淀 → P5 释放锁并拉起队首
    TODO(M7): 委托 mining.service.run_mining_pipeline。
    """
    raise NotImplementedError("M7: run_mining_task")


def run_validation_task(task_id: str) -> None:
    """字段异步校验。按「字段 × 时间段」分片，支持断点续跑。

    **不占 mining_domain**（设计文档 §7.1）。
    TODO(M15): 委托 mining.validation_service.run_validation。
    """
    raise NotImplementedError("M15: run_validation_task")


def run_mirror_task(task_id: str) -> None:
    """历史日线镜像。持 duckdb_write，watermark 分块续跑。

    复用 `bar_mirror._mirror_universe_bars(start_date, end_date)`。
    TODO(M1): 委托 mirror_task.run_mirror。
    """
    raise NotImplementedError("M1: run_mirror_task")


def run_grade_review_task(task_id: str) -> None:
    """季度重评（M2）。由 scheduled_tasks 的 factor_grade_review 触发。

    TODO(M13): 委托 factor_grading.run_quarterly_review。
    """
    raise NotImplementedError("M13: run_grade_review_task")


# ══════════════════════════════════════════════════════════
# 心跳辅助（更新点 = 每代结束 + 最终验证每完成 10 个因子）
# ══════════════════════════════════════════════════════════


def beat_after_generation(*, run_id: str, task_id: str, generation: int, total: int) -> None:
    update_heartbeat(task_id)
    update_progress(task_id, processed=generation, total=total, stage="evolution")
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        mining_lock.heartbeat(db, lock_key=mining_lock.LOCK_DUCKDB_WRITE, task_id=task_id)
    finally:
        db.close()


def beat_every_10_factors(*, task_id: str, done: int, total: int) -> None:
    if done % 10 != 0:
        return
    update_heartbeat(task_id)
    update_progress(task_id, processed=done, total=total, stage="final_evaluation")
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        mining_lock.heartbeat(db, lock_key=mining_lock.LOCK_DUCKDB_WRITE, task_id=task_id)
    finally:
        db.close()


__all__ = [
    "TASK_TYPE_MINING",
    "TASK_TYPE_VALIDATION",
    "TASK_TYPE_MIRROR",
    "TASK_TYPE_GRADE_REVIEW",
    "submit_mining_task",
    "run_mining_task",
    "run_validation_task",
    "run_mirror_task",
    "run_grade_review_task",
    "beat_after_generation",
    "beat_every_10_factors",
]
