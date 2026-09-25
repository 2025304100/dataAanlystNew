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
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy.exc import InterfaceError as _SAInterfaceError
from sqlalchemy.exc import OperationalError as _SAOperationalError

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
        """worker 应停止吗（DEF-12/DEF-3）：

        ① 进程级优雅停机事件（既有语义）；
        ② 任务被请求取消（`cancel_requested=1`，由 cancel API / 看门狗 reap 写入）。
        ② 走 DB 但带 5s 节流缓存，逐个体调用不构成压力。
        """
        if is_worker_stop_requested(task_id=self.task_id):
            return True
        return _cancel_requested_cached(self.task_id)


# ══════════════════════════════════════════════════════════
# DEF-12：代内推进信号 + 取消请求感知
# ══════════════════════════════════════════════════════════

#: 代内评估 touch `run.updated_at` 的最小间隔（秒）。
#: 30s 节流下每分钟至多 2 次轻量 UPDATE（控制平面连接），远低于看门狗 600s 阈值。
TOUCH_RUN_MIN_INTERVAL = 30.0

#: `cancel_requested` DB 复查间隔（秒）——逐个体 should_stop 调用的节流。
CANCEL_CHECK_INTERVAL = 5.0

_touch_state: dict[str, float] = {}
_touch_guard = threading.Lock()
_cancel_state: dict[str, tuple[float, bool]] = {}
_cancel_guard = threading.Lock()


def touch_run_progress(
    run_id: str, *, min_interval: float = TOUCH_RUN_MIN_INTERVAL
) -> bool:
    """**代内评估推进信号**：轻量刷新 `run.updated_at`（节流）。

    为什么需要（DEF-12，2026-09-24 实测）：停滞看门狗以 `run.updated_at`
    为唯一推进信号，但代内评估只在每代末 `sync_run_progress` 时刷新它 ——
    大种群（前端默认 100×20）单代评估实测 >10 分钟，正常任务必被误杀
    STALLED 且锁被放、worker 却还在跑。本函数由个体评估回调周期性调用，
    让看门狗看到「仍在推进」；单个个体内部卡死（真停滞）时无 touch、
    仍会被正常回收，两不耽误。

    Returns:
        是否真的执行了 touch（节流窗口内返回 False）。
    """
    now = time.monotonic()
    with _touch_guard:
        last = _touch_state.get(run_id, 0.0)
        if now - last < float(min_interval):
            return False
        _touch_state[run_id] = now
    try:
        from sqlalchemy import update as _sa_update

        from app.db.session import get_control_session_local
        from app.models.factor_mining import FactorMiningRun

        db = get_control_session_local()()
        try:
            db.execute(
                _sa_update(FactorMiningRun)
                .where(FactorMiningRun.id == run_id)
                .values(updated_at=_now())
            )
            db.commit()
        finally:
            db.close()
        return True
    except Exception:  # noqa: BLE001 - 推进信号失败不影响挖掘主流程
        logger.debug("touch_run_progress failed for run %s", run_id,
                     exc_info=True)
        return False


def _cancel_requested_cached(task_id: str) -> bool:
    """带节流缓存地读 `async_task_records.cancel_requested`。

    任务记录不存在 ⇒ 返回 False（保守：测试注入/直跑场景不误杀）。
    """
    now = time.monotonic()
    with _cancel_guard:
        hit = _cancel_state.get(task_id)
        if hit is not None and now - hit[0] < CANCEL_CHECK_INTERVAL:
            return hit[1]
    requested = False
    try:
        from app.db.session import get_control_session_local

        db = get_control_session_local()()
        try:
            row = db.get(AsyncTaskRecord, task_id)
            requested = False if row is None else bool(
                getattr(row, "cancel_requested", 0) or 0)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 读不到取消标志按 False 处理，别误杀
        requested = False
    with _cancel_guard:
        _cancel_state[task_id] = (now, requested)
    return requested


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


def _set_task_fresh(task_id: str, **updates) -> None:
    """用新会话 + 断连重试写任务终态（DEF-9 稳定性，2026-09-25 实测）。

    worker 的长寿命 session 跑完数十分钟进化后常被 MySQL wait_timeout 掉线
    （2006/2013），直接拿它写 done/cancelled 会 crash → 任务结果丢失、
    前端永远看不到 finalize 信息。run_in_retry_session 自带重建重试。
    """
    from app.db.manager import DatabaseManager

    def _runner(session, do_commit):
        _set_task(session, task_id, **updates)
        do_commit()

    DatabaseManager.get().run_in_retry_session(_runner)


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
            _set_task_fresh(task_id, status="cancelled", stage="cancelled",
                            message="cancelled before stages", finished_at=_now())
            return

        runner = stage_runner or _default_stage_runner
        result = runner(ctx) or {}

        if ctx.should_stop():
            _set_task_fresh(task_id, status="cancelled", stage="cancelled",
                            message="cancelled after stages", finished_at=_now())
            return

        _set_task_fresh(
            task_id,
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
        _release_all_locks(task_id, resume_runner=stage_runner)
        db.close()


def skeleton_stage_runner(ctx: MiningWorkerContext) -> dict[str, Any]:
    """**轻量骨架阶段**（M1a 语义，仅供基础设施生命周期联调）。

    不依赖任何 run 数据，只验证 心跳/双锁/排队/状态机 走通；
    生产默认阶段链是 `_default_stage_runner`（真实挖掘）。
    """
    ctx.heartbeat()
    return {"message_zh": "挖掘骨架完成（M1a 基础设施验证；GA 循环在 M1b 接入）"}


def _resolve_selection_mode(evo: Mapping[str, Any]) -> str:
    """推导 GA 选择路径（P0：让前端已提交的 M2 配置真实生效）。

    - 显式 `selection_mode`（"naive"/"advanced"）优先 —— 回退与复现基准；
    - 否则出现任一 M2 配置键且为真值（前端 Step4 默认提交 `adaptive: true`）
      → `advanced`（A1/B1/B2 + C1-C3 + D1-D3 全链路）；
    - 完全不带 M2 键的提交（API 脚本/既有测试）→ `naive`（M1 行为不变）。
    """
    explicit = str(evo.get("selection_mode") or "").strip().lower()
    if explicit in ("naive", "advanced"):
        return explicit
    m2_keys = ("adaptive", "tournament_k", "objectives",
               "cross_category_ratio", "mutation_ops_enabled")
    if any(k in evo and evo.get(k) for k in m2_keys):
        return "advanced"
    return "naive"


def _fill_random_exploration(
    population: list[dict[str, Any]], *, target_size: int,
    selected_fields: Sequence[str], seed: int,
) -> int:
    """受约束随机探索层（P0：两层结构第二层补齐）。

    经典底座 + AI 之后的剩余名额由受约束随机公式填充（向导 §6.3.6/§6.3.7：
    「AI 不足随机补，都不足允许种群略小于目标」）。生成器与 D2 注入同源
    （`random_generator`）；失败返回 0 不阻断（种群允许略小于目标值）。
    """
    from app.services.factors.mining.random_generator import (
        RandomGeneratorConfig,
        generate_random_candidates,
    )

    remaining = max(0, int(target_size) - len(population))
    if remaining <= 0:
        return 0
    existing = tuple(
        str(p.get("canonical_formula") or p.get("formula") or "")
        for p in population)
    try:
        result = generate_random_candidates(
            cfg=RandomGeneratorConfig(target_count=remaining, seed=int(seed)),
            selected_fields=list(selected_fields),
            existing_formulas=list(existing),
        )
    except Exception:  # noqa: BLE001 - 随机层失败不阻断挖掘
        return 0
    added = 0
    for item in (getattr(result, "candidates", None) or []):
        entry = dict(item)
        entry["operation"] = "random"
        entry["generation"] = 0
        if not entry.get("canonical_formula"):
            entry["canonical_formula"] = str(entry.get("formula") or "")
        if not entry.get("formula_hash"):
            entry["formula_hash"] = entry["canonical_formula"]
        population.append(entry)
        added += 1
    return added


def _make_random_supplier(
    *, selected_fields: Sequence[str], base_seed: int,
    existing_formulas: Sequence[str],
) -> Callable[[int], list[dict[str, Any]]]:
    """D2 随机注入供给（`run_ga_loop.random_supplier` 注入点）。

    每次按计数生成受约束随机公式（种子由 base_seed 派生，固定可复现）；
    记忆已见公式避免代间重复。失败返回空列表 → GA 用变异兜底，不阻断进化。
    """
    from app.services.factors.mining.random_generator import (
        RandomGeneratorConfig,
        generate_random_candidates,
    )

    seen: set[str] = {str(f) for f in existing_formulas if str(f)}
    calls = {"n": 0}

    def _supply(count: int) -> list[dict[str, Any]]:
        count = max(0, int(count))
        if count <= 0:
            return []
        calls["n"] += 1
        try:
            result = generate_random_candidates(
                cfg=RandomGeneratorConfig(
                    target_count=count,
                    seed=int(base_seed) + calls["n"] * 977),
                selected_fields=list(selected_fields),
                existing_formulas=list(seen),
            )
        except Exception:  # noqa: BLE001 - 注入失败 → 变异兜底
            return []
        out: list[dict[str, Any]] = []
        for item in (getattr(result, "candidates", None) or []):
            entry = dict(item)
            formula = str(entry.get("canonical_formula")
                          or entry.get("formula") or "")
            if formula:
                seen.add(formula)
            out.append(entry)
        return out

    return _supply


def _default_stage_runner(ctx: MiningWorkerContext) -> dict[str, Any]:
    """**真实挖掘阶段链**（任务卡 A2，替换 M1a 基础设施骨架）。

    流程：目标标签生成 → `initial_population`（经典模板底座 + AI + 随机探索层）→
    `run_ga_loop`（**真实** `evaluate_short`，train 段/G2/探针；M2 配置生效）→
    `finalize_run`（`payload.finalize_top_k=0` 可跳过，供分阶段联调）。

    - 每代 `persist_generation` + `persist_candidates` + `sync_run_progress` + 探针落库；
    - 心跳/双锁语义由 worker 既有机制负责（本函数只调用 `ctx.heartbeat()`）；
    - 单个体评估失败由 GA 主循环隔离（沉底淘汰），**不中断整代**。
    """
    return _run_real_stages(ctx)


def _fitness_dict(fitness: Any) -> dict[str, Any]:
    """`Fitness` → dict（`run_ga_loop` 的 evaluate 回调要求 Mapping，内含 `icir`）。"""
    return {
        "icir": getattr(fitness, "icir", 0.0),
        "coverage": getattr(fitness, "coverage", 0.0),
        "turnover": getattr(fitness, "turnover", 0.0),
        "ic_mean": getattr(fitness, "ic_mean", 0.0),
        "complexity": getattr(fitness, "complexity", 0),
        "valid_cross_sections": getattr(fitness, "valid_cross_sections", 0),
        "sample_count": getattr(fitness, "sample_count", 0),
    }


def _to_individual(item: Mapping[str, Any]) -> Any:
    """种群 dict → `contracts.Individual`（`execution_plan` 用不到，置 None）。"""
    from app.services.factors.mining.contracts import Individual

    formula = str(item.get("canonical_formula") or item.get("formula") or "")
    return Individual(
        formula_expr=str(item.get("formula") or formula),
        canonical_formula=formula,
        formula_hash=str(item.get("formula_hash") or "") or formula,
        execution_plan=None,
        dependency=None,
        category=item.get("category"),
        generation=int(item.get("generation") or 0),
        parent_ids=tuple(str(p) for p in (item.get("parent_ids") or ())),
        operation=item.get("operation") or "enumerated",
        complexity=int(item.get("complexity") or (formula.count("("))),
        economic_logic=item.get("economic_logic"),
        expected_direction=item.get("expected_direction"),
        logic_source=item.get("logic_source"),
    )


def _run_real_stages(ctx: MiningWorkerContext) -> dict[str, Any]:
    """真实阶段链实现（见 `_default_stage_runner` docstring）。"""
    from app.core.config import Settings
    from app.services.factors.mining import (
        ai_generator as AIG,
        evaluation_adapter as EVA,
        genetic_algorithm as GA,
        initial_population as IP,
        performance_probe as PROBE,
        runtime_correlation as RC,
        service as SVC,
        subexpr_cache as SC,
    )
    from app.services.factors.mining.contracts import MiningContext
    from app.services.factors.store import FactorWarehouse
    from app.services.factors.target_engine import calculate_targets
    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        run = SVC.load_run(db, ctx.run_id)
        if run is None:
            raise ValueError(f"run 不存在: {ctx.run_id!r}")
        payload = dict(ctx.payload or {})
        freq = str(run.rebalance_frequency or "daily")
        horizon = int(getattr(run, "target_horizon", 0) or 5)
        # 立即结束 `load_run` 开启的隐式事务并归还连接：目标计算与逐代进化
        # 都是分钟级纯 DuckDB 计算，期间若继续持有 MySQL 连接，会被
        # `wait_timeout`（实测本机 120s）断开，首代持久化即 2006
        # （eval.db.mysql_gone_away_2006）→ worker crash。归还后由
        # 连接池 pool_pre_ping/recycle=60s 在下次 checkout 时自动刷新。
        db.rollback()

        # ① 目标标签：按 run 区间生成（worker 已持 duckdb_write）
        wh = FactorWarehouse(str(
            payload.get("warehouse_path") or Settings().factor_warehouse_path))
        calc_batch = f"mining-{ctx.run_id}"
        calculate_targets(
            wh, start_date=run.start_date.date(), end_date=run.end_date.date(),
            calc_batch_id=calc_batch,
        )

        # ② MiningContext（切分走归一化入口；候选池过滤留 A5）
        target_df, _bid, _tcode = wh.get_target_panel(calc_batch, "target_5d_return")
        all_dates = sorted(
            d for d in (EVA._as_date(v) for v in target_df["signal_date"].tolist())
            if d is not None
        )
        split, budget = EVA.build_split(
            all_dates=all_dates, frequency=freq, target_horizon=horizon)
        mc = MiningContext(
            run_id=run.id, candidate_pool_snapshot_id=str(run.candidate_pool_snapshot_id),
            data_cutoff_at=run.data_cutoff_at, start_date=run.start_date.date(),
            end_date=run.end_date.date(), rebalance_frequency=freq,
            target_horizon=horizon, split=split,
            purge_points=budget.purge_points, embargo_points=budget.embargo_points,
            train_ratio=0.6, validation_ratio=0.2,
            random_seed=int(getattr(run, "random_seed", 0) or 42),
            config_hash=str(getattr(run, "config_hash", "") or ""),
            split_algorithm_version=str(getattr(run, "split_algorithm_version", "")
                                        or budget and "split-1.0.0"),
            target_calc_batch_id=calc_batch,
            warehouse_path=str(wh.path),
            data_snapshot_version=str(getattr(run, "split_algorithm_version", "")
                                      or "snapshot-default"),
        )

        # ③ 初始种群（经典模板底座；AI/随机探索层接入 A3）
        evo = dict(payload.get("evolution_params") or {})
        pop_size = max(4, int(evo.get("population_size", 60) or 60))
        max_gen = max(1, int(evo.get("max_generations", 8) or 8))
        fields = list(payload.get("selected_fields") or [
            "close", "open", "high", "low", "volume", "amount", "turnover_rate"])
        cats = list(evo.get("enabled_categories") or [
            "trend", "reversal", "volatility", "volume_price"])
        try:
            result = IP.build_initial_population(
                population_size=pop_size, selected_fields=fields,
                enabled_categories=cats,
                template_limit=evo.get("classic_template_limit"))
        except Exception as exc:  # noqa: BLE001 - 模板/字段配置异常 → 阻断本任务
            raise RuntimeError(f"初始种群生成失败: {type(exc).__name__}: {exc}") from exc
        population: list[dict[str, Any]] = []
        for row in (result.candidates or []):
            formula = str(row.get("formula") or "")
            if not formula:
                continue
            population.append({
                **dict(row),
                "canonical_formula": formula,
                "formula_hash": str(row.get("formula_hash") or "") or formula,
                "operation": "enumerated",
                "generation": 0,
                "source": "template",
                "economic_logic": row.get("economic_logic") or row.get("economy_logic_zh"),
            })
        if not population:
            logger.warning(
                "初始种群经典层为空 run=%s（字段=%s），等待 AI/随机探索层补位",
                run.id, fields)

        # ③.5 AI 层填充剩余名额（`ai_enabled` 显式开才走；尽力而为，失败不阻断）
        #     AIResult 契约：永不抛异常；accepted 条目自带 formula/formula_hash/
        #     operation=ai_generated/economic_logic 等完整字段（验收报告 #16）。
        if bool(evo.get("ai_enabled")):
            remaining = max(0, pop_size - len(population))
            if remaining > 0:
                try:
                    ai_cfg = AIG.AIGeneratorConfig(
                        target_count=min(remaining, 12),
                        selected_fields=tuple(fields),
                        enabled_categories=tuple(cats),
                        existing_formulas=tuple(
                            str(p["canonical_formula"]) for p in population),
                        seed=int(evo.get("random_seed", 0)
                                 or int(getattr(run, "random_seed", 0) or 42)),
                    )
                    ai_res = AIG.generate_ai_candidates(ai_cfg, db=db)
                    for _item in ai_res.candidates:
                        population.append(_item)
                    if ai_res.candidates:
                        logger.info(
                            "AI 初始种群填充 run=%s: +%d 个（来源 ai_generated）",
                            run.id, len(ai_res.candidates))
                except Exception as exc:  # noqa: BLE001 - AI 填充失败不阻断挖掘
                    logger.warning("AI 初始种群填充失败 run=%s: %s", run.id, exc)

        # ③.6 受约束随机探索层（P0：两层结构第二层补齐——经典+AI 之后的
        #     剩余名额由随机填充；AI 关闭/失败时由本层兜底，种群不塌缩）。
        remaining = max(0, pop_size - len(population))
        if remaining > 0:
            random_added = _fill_random_exploration(
                population, target_size=pop_size, selected_fields=fields,
                seed=int(evo.get("random_seed", 0)
                         or int(getattr(run, "random_seed", 0) or 42)))
            if random_added:
                logger.info(
                    "随机探索层填充 run=%s: +%d 个（经典+AI 后剩余名额）",
                    run.id, random_added)

        # ③.7 三来源全部为空才阻断（设计 §6.3：经典保底、AI/随机补位，均失败才失败）。
        #     经典层为空但随机层可用时**不阻断**——随机层基于所选字段的受约束公式
        #     （如 cs_rank(field)）恒可生成，保证种群不塌缩、run 贯通到结果页。
        if not population:
            raise RuntimeError(
                "初始种群为空：经典模板与 AI/随机探索在所选字段（"
                f"{', '.join(str(f) for f in fields[:8])}"
                f"{'…' if len(fields) > 8 else ''}）下均未生成有效候选。"
                "Step3 应选择行情/估值/财报等 DSL 已注册字段（如 close/pe_ttm），"
                "而不是候选池筛选字段（如 avg_amount/avg_volume），否则经典模板无法展开。")

        # ④ GA 主循环（真实 evaluate_short；每代持久化 + 探针落库）
        # 进入前结束任何悬空事务（初始种群/AI 层可能已开隐式事务），
        # 确保评估阶段不持有 MySQL 连接（防 wait_timeout 2006）。
        db.rollback()
        ga_cfg = GA.GAConfig(
            population_size=pop_size, max_generations=max_gen,
            selection_ratio=max(0.05, float(evo.get("selection_ratio", 0.3) or 0.3)),
            mutation_rate=float(evo.get("mutation_rate", 0.55) or 0.55),
            crossover_rate=float(evo.get("crossover_rate", 0.25) or 0.25),
            random_rate=float(evo.get("random_rate", 0.20) or 0.20),
            convergence_threshold=float(evo.get("convergence_threshold", 1e-3) or 1e-3),
            convergence_generations=int(evo.get("convergence_generations", 2) or 2),
            seed=int(evo.get("random_seed", 0) or int(getattr(run, "random_seed", 0) or 42)),
            # ── M2 选择/繁殖（P0：前端已提交的配置现在真实生效） ──
            selection_mode=_resolve_selection_mode(evo),
            objectives=(tuple(str(o) for o in evo["objectives"])
                        if evo.get("objectives") else None),
            tournament_k=int(evo.get("tournament_k", 3) or 3),
            adaptive=bool(evo.get("adaptive")),
            cross_category_ratio=float(evo.get("cross_category_ratio", 0.2) or 0.2),
            mutation_ops_enabled=(
                tuple(str(k) for k in evo["mutation_ops_enabled"])
                if evo.get("mutation_ops_enabled") else None),
            selected_fields=tuple(fields) or None,
        )
        sample_len = str(payload.get("sample_length") or "1y")

        # ── 性能（P5 收敛）：task 级共享 G2 缓存 + target 面板一次化 + 校验抽样 ──
        # 此前每个 evaluate 新建 SubexpressionCache，50 个体/代间子表达式零复用，
        # 且每次 evaluate 都全量直算比对（verify_sample_size=1）。现改为：
        #   * CacheRegistry 按 (scope, snapshot) 共享一个缓存 → 唯一子式只算一次；
        #   * 评估计数取模抽样校验（每 5 个因子验 1 个，守住 §6.11.1 兜底语义）。
        cache_reg = SC.CacheRegistry(task_id=run.id)
        snapshot_ver = str(getattr(mc, "data_snapshot_version", None) or "snapshot-default")
        cache_access = {"seq": 0}
        per_gen_probe: list[Any] = []

        def _evaluate(item: Mapping[str, Any]) -> dict[str, Any]:
            # DEF-12：代内推进信号——大种群单代评估可超 10 分钟，
            # 不刷新 updated_at 会被停滞看门狗误杀（STALLED）。
            touch_run_progress(run.id)
            cache = cache_reg.for_scope(SC.SCOPE_PRESCREEN, data_snapshot_version=snapshot_ver)
            verify = 1 if (cache_access["seq"] % 5 == 0) else 0
            cache_access["seq"] += 1
            fit, probe, _cache, sig = EVA.evaluate_short_signed(
                mc, individual=_to_individual(item), sample_length=sample_len,
                cache=cache, verify_sample_size=verify,
            )
            per_gen_probe.append(probe)
            out = _fitness_dict(fit)
            out["signature"] = sig
            return out

        def _runtime_dedup(ranked: Sequence[Mapping[str, Any]]) -> Any:
            """第 4 层相关性去重（P1-6；naive/advanced 均启用）。"""
            return RC.dedup_by_correlation(
                ranked, seed=int(evo.get("random_seed", 0)
                                 or int(getattr(run, "random_seed", 0) or 42)))

        def _persist_generation_once(gen: int, record: Mapping[str, Any],
                                 ranked: Sequence[Mapping[str, Any]]) -> None:
            SVC.persist_generation(db, run_id=run.id, generation=gen, record=record)
            SVC.persist_candidates(db, run_id=run.id, generation=gen, ranked=ranked)
            SVC.sync_run_progress(db, run_id=run.id, generation=gen,
                                  total_trials=int(record.get("total_trials") or 0))
            if per_gen_probe:
                try:
                    PROBE.write_generation_probe(
                        db, run_id=run.id, generation=gen, probe=per_gen_probe[-1])
                except Exception as exc:  # noqa: BLE001 - 探针落库失败不中断进化
                    logger.warning("探针落库失败 run=%s gen=%s: %s",
                                   run.id, gen, exc)
            # 每代立即提交：结束隐式事务、归还连接（配合 pool_pre_ping/
            # recycle=60s 防 wait_timeout 断连），并保证 checkpoint 可持久。
            db.commit()

        def _on_generation(gen: int, record: Mapping[str, Any],
                           ranked: Sequence[Mapping[str, Any]]) -> None:
            nonlocal db
            # MySQL 连接抖动（2006/2013/InterfaceError）兜底：rollback+重建
            # session 后重试，最多 3 次。每次重建都会从连接池 checkout 新连接
            # （pool_pre_ping 丢弃失效连接），环境性抖动后大概率成功。
            for attempt in range(3):
                try:
                    _persist_generation_once(gen, record, ranked)
                    break
                except (_SAOperationalError, _SAInterfaceError) as exc:
                    text = str(exc).lower()
                    is_conn_lost = (
                        "2006" in text or "2013" in text
                        or "gone away" in text or "lost connection" in text
                        or "interfaceerror" in type(exc).__name__.lower())
                    if not is_conn_lost:
                        raise
                    if attempt == 2:
                        raise
                    logger.warning(
                        "MySQL 连接异常 gen=%s run=%s（%s），重连重试 %s/2",
                        gen, run.id, type(exc).__name__, attempt + 1)
                    try:
                        db.rollback()
                    except Exception:  # noqa: BLE001 - 连接已损坏，丢弃重建
                        db.close()
                        db = get_session_local()()
            ctx.heartbeat()

        outcome = GA.run_ga_loop(
            ga_cfg, population, evaluate=_evaluate,
            random_supplier=_make_random_supplier(
                selected_fields=fields, base_seed=ga_cfg.seed,
                existing_formulas=tuple(
                    str(p.get("canonical_formula") or p.get("formula") or "")
                    for p in population)),
            on_generation=_on_generation,
            runtime_dedup=_runtime_dedup,
            # DEF-12/DEF-3：逐个体协作式停止——pause/看门狗请求后 worker 自止
            should_stop=ctx.should_stop,
        )

        # 停止请求后不再继续 finalize（run 可能已被看门狗置 failed）；
        # worker 外层会把 task 置 cancelled 并释放锁。
        if ctx.should_stop():
            return {
                "message_zh": "已按停止请求提前结束（未执行最终验证）",
                "generations": outcome.generations_run,
                "stopped_reason": outcome.stopped_reason,
                "best_icir": outcome.best_icir,
                "candidates": len(population),
                "total_trials": outcome.total_trials,
                "finalize_top_k": max(0, int(payload.get("finalize_top_k", 50) or 0)),
                "final": None,
            }

        # ⑤ 最终验证（test-once；finalize_top_k=0 时跳过，仅收尾状态）
        final_top_k = max(0, int(payload.get("finalize_top_k", 50) or 0))
        final: dict[str, Any] | None = None
        if final_top_k > 0:
            # DEF-9 稳定性（2026-09-25 V4a 实测）：长进化后 worker 的 MySQL
            # 会话常被 wait_timeout 掉线（2006/2013），finalize 若拿着死连接
            # 会把整批候选 evaluate_full 吞成 failed → 结果页永不可达。
            # 收官前自检，断连则重建会话（run 行由 finalize 按 id 重读）。
            try:
                db.execute(_sa_text("SELECT 1")).close()
            except Exception:  # noqa: BLE001 - 任何断连/坏事务都重建
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
                db = get_session_local()()
            final = SVC.finalize_run(db, ctx=mc, run_id=run.id, top_k=final_top_k)
        else:
            if run.status not in ("succeeded",):
                SVC.set_run_status(db, run, "succeeded")
            run.converged = 1
            db.commit()

        return {
            "message_zh": "真实挖掘阶段完成",
            "generations": outcome.generations_run,
            "stopped_reason": outcome.stopped_reason,
            "best_icir": outcome.best_icir,
            "candidates": len(population),
            "total_trials": outcome.total_trials,
            "finalize_top_k": final_top_k,
            "final": final,
        }
    finally:
        db.close()


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


def _release_all_locks(task_id: str, *, resume_runner: StageRunner | None = None) -> str | None:
    """释放双锁；`duckdb_write` 拉起队首并**唤醒**它（启动其 worker）。

    被拉起的任务沿用 `resume_runner`（缺省 = 真实阶段链）：同批提交注入的
    runner 会透传到队首任务（测试注入骨架/自定义时保持语义一致）。
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
        _start_worker(next_task_id, _make_worker(resume_runner))
    return next_task_id


def _verify_holds_write(db: Any, task_id: str) -> bool:
    status = TL.get_lock_status(db)
    return status.duckdb_write.get("taskId") == task_id


def resume_pending_mining_tasks(
    stage_runner: StageRunner | None = None,
) -> list[str]:
    """补拉滞留在 queued 的挖掘任务（运维/应用启动时调用）。

    场景：持有者在「释放锁」与「唤醒队首」之间崩溃 → 队首任务滞留 queued。
    这里扫描 queued/running 的挖掘任务：若它持有 `duckdb_write` 但没有
    存活的 worker（无法直接判定，用「任务处于 queued 且锁行 owner 是它」
    近似），补启动 worker。

    `stage_runner` 缺省用真实阶段链；基础设施测试可注入 `skeleton_stage_runner`。
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
                _start_worker(task.id, _make_worker(stage_runner))
                resumed.append(task.id)
    finally:
        db.close()
    return resumed


def _dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


from sqlalchemy import select  # noqa: E402  （resume_pending_mining_tasks 使用）
from sqlalchemy import text as _sa_text  # noqa: E402  （finalize 前连接自检）


__all__ = [
    "TASK_TYPE",
    "TASK_HEARTBEAT_SECONDS",
    "TOUCH_RUN_MIN_INTERVAL",
    "CANCEL_CHECK_INTERVAL",
    "MiningSubmitResult",
    "MiningWorkerContext",
    "skeleton_stage_runner",
    "submit_mining_run",
    "run_mining_worker",
    "heartbeat_mining",
    "touch_run_progress",
    "resume_pending_mining_tasks",
]
