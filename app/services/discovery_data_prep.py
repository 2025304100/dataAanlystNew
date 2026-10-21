"""WP-P.4 后台数据准备链。

将"行情同步 → 外部因子更新 → 因子评分增量计算 → 评分快照生成"这一长耗时链路
从用户扫描路径中剥离，作为独立的后台异步任务执行（不受 5 分钟 SLA 约束）。

核心约束（参照 project_memory 硬约束）：
1. 同一 scope 同时只允许一个 data_prep 任务运行（并发保护）
2. 关键数据操作（快照状态切换）必须 commit 后再继续后续处理
3. 异步任务终态（cancelled / failed / done）不被 worker 覆盖（依赖 _set_task 终态保护）
4. 进度更新单步不超过 30%（参照 task_state_machine.MAX_PROGRESS_STEP）
5. 后台数据准备可调用现有同步任务（market_data_sync / financial_report_task /
   macro_update_task / hot_rank_task / lhb_institution_task / tail_proxy_task /
   factors/pipeline_task），与现有稳定逻辑解耦
6. 不重写已稳定的 start_discovery / 异步任务基础逻辑
"""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
from threading import Lock, get_ident
from datetime import date, datetime, time as clock_time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.schemas.async_task import AsyncTaskRead
from app.services import async_tasks
from app.services.async_tasks import _append_error, _now, _set_task, _start_worker, create_async_task
from app.services.discovery_dirty_set import (
    compute_dirty_symbols,
    is_snapshot_building_for_scope,
    mark_snapshot_superseded,
    should_trigger_full_rebuild,
)
from app.services.task_state_machine import (
    enter_stage,
    update_heartbeat,
    update_progress,
)
from app.services.discovery_stage_budget import CancelToken

logger = logging.getLogger(__name__)

TASK_TYPE_DATA_PREP = "discovery_data_prep"
_PREPARATION_DISPATCH_LOCK = Lock()


class _PreparationCancelled(Exception):
    pass


def _ensure_preparation_active(task_id: str) -> None:
    state = async_tasks.get_async_task(task_id)
    if (state is None or state.cancel_requested or state.status in {"cancelled", "failed", "done", "partial"}
            or async_tasks.is_worker_stop_requested()):
        raise _PreparationCancelled()


class _PreparationScanCancelToken(CancelToken):
    def check(self) -> None:
        try:
            _ensure_preparation_active(self.task_id)
        except _PreparationCancelled:
            self.cancel()
        super().check()


def _complete_preparation(db: Session, task_id: str, *, scanned: bool) -> None:
    _ensure_preparation_active(task_id)
    task = db.get(AsyncTaskRecord, task_id)
    has_warnings = bool(async_tasks._json_loads(task.errors_json if task else None, []))
    has_intraday = (async_tasks._json_loads(task.result_json if task else None, {}) or {}).get("source_data_status") == "intraday"
    message = "准备与扫描已完成" if scanned else "数据准备完成"
    if has_warnings:
        message += "，部分外部数据更新失败"
    if has_intraday:
        message += "，包含盘中数据（非收盘评分）"
    _set_task(db, task_id, status="partial" if has_warnings or has_intraday else "done", stage="done",
              percent=100, message=message, finished_at=_now())


def _fail_unpublished_snapshots(db: Session, task_id: str, reason: str) -> None:
    """Retain interrupted versions without leaving a building lock behind."""
    snapshots = db.scalars(select(DiscoveryScoreSnapshot).where(
        DiscoveryScoreSnapshot.source_task_id == task_id,
        DiscoveryScoreSnapshot.status == "building",
    )).all()
    for snapshot in snapshots:
        snapshot.status = "failed"
        snapshot.error_summary_json = json.dumps({"error": reason}, ensure_ascii=False)
    if snapshots:
        db.commit()


def _recover_abandoned_snapshots(db: Session, scope: str) -> None:
    abandoned = False
    for snapshot in db.scalars(select(DiscoveryScoreSnapshot).where(
        DiscoveryScoreSnapshot.scope == scope,
        DiscoveryScoreSnapshot.status == "building",
    )).all():
        source = db.get(AsyncTaskRecord, snapshot.source_task_id) if snapshot.source_task_id else None
        if source is not None and source.status not in {"done", "partial", "failed", "cancelled"}:
            continue
        snapshot.status = "failed"
        snapshot.error_summary_json = json.dumps({"error": "来源任务已终止或不存在，保留未发布版本供追溯"}, ensure_ascii=False)
        abandoned = True
    if abandoned:
        db.commit()


def _prepared_score_dates(db: Session, symbol_ids: list[int], score_batch_ids: list[str] | None = None) -> dict[int, date]:
    """Require an actual active-scope score for every prepared symbol's latest bar."""
    from app.services.factors.__facade__ import apply_active_score_scope_to_scores_select

    if not symbol_ids:
        raise RuntimeError("本轮没有已准备的行情标的，未发布新快照")
    bars = dict(db.execute(select(DailyBar.symbol_id, func.max(DailyBar.trade_date)).where(
        DailyBar.symbol_id.in_(symbol_ids),
    ).group_by(DailyBar.symbol_id)).all())
    statement = select(Score.symbol_id, func.max(Score.trade_date)).where(Score.symbol_id.in_(symbol_ids))
    if score_batch_ids is not None:
        statement = statement.where(Score.calc_batch_id.in_(score_batch_ids))
    scores = dict(db.execute(apply_active_score_scope_to_scores_select(
        statement.group_by(Score.symbol_id), db,
    )).all())
    invalid = [sid for sid in symbol_ids if not bars.get(sid) or bars[sid] > date.today()
               or scores.get(sid) != bars[sid]]
    if invalid:
        raise RuntimeError(f"本轮实际评分日与最新行情不一致或缺失，未发布新快照：{invalid[:10]}")
    return scores


def _wait_for_preparation_child(task_id: str, child, stage: str, db: Session) -> dict | None:
    """Wait for the real persisted child, rather than treating dispatch as completion."""
    db.rollback()
    if child is None:
        return None
    child_id = child.get("id") if isinstance(child, dict) else getattr(child, "id", None)
    if not child_id:
        raise RuntimeError(f"{stage}: 子任务未返回任务编号")
    parent = db.get(AsyncTaskRecord, task_id)
    summary = async_tasks._json_loads(parent.result_json if parent else None, {}) or {}
    summary.setdefault("child_task_ids", {})[stage] = child_id
    try:
        _set_task(db, task_id, result_json=json.dumps(summary, ensure_ascii=False, default=str))
    finally:
        # _set_task commits, then refreshes its row. End that read transaction
        # before the potentially long supplier wait; polling owns short sessions.
        db.rollback()
    deadline = time.monotonic() + _STAGE_BUDGET_SECONDS.get(stage.split(".")[0], 1800)
    last_heartbeat = 0.0
    while True:
        parent_state = async_tasks.get_async_task(task_id)
        if (parent_state is None or parent_state.cancel_requested
                or parent_state.status == "cancelled" or async_tasks.is_worker_stop_requested()):
            raise _PreparationCancelled()
        state = async_tasks.get_async_task(str(child_id))
        if state is None:
            raise RuntimeError(f"{stage}: 子任务 {child_id} 不存在")
        if state.status == "done" and not state.failed_count:
            return state.model_dump(mode="json")
        if state.status in {"done", "partial", "failed", "cancelled"}:
            raise RuntimeError(f"{stage}: 子任务 {child_id} 未成功（{state.status}）：{state.message}")
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(f"{stage}: 等待子任务 {child_id} 超时，请在任务中心查看")
        if now - last_heartbeat >= 2:
            update_heartbeat(task_id, current_step_description=f"等待 {stage}：{state.message}")
            last_heartbeat = now
        async_tasks.WORKER_STOP_EVENT.wait(0.2)

# PT-DEF-15：“无 ready 快照 → 自动启动数据准备”的开关（默认开，保持现有行为）。
# 关掉后：快扫只回 degraded_reason="no_ready_snapshot"，不再隐式起后台任务，
# 也不再真打第三方行情源；手动触发数据准备的接口不受影响。
# 优先级：**app_settings 表（用户在设置页改的）> 环境变量 > 默认开**。
# 保留 env 通道是为了运维级强制关闭（整环境禁外网、CI 里不碰行情源），
# 且表未迁移时能自然回退（读不到就不拦）。
# env 名不在本模块另写一份：由 app_settings 的登记表持有（单一真相），
# 本处只是保留旧导入路径的别名，供既有调用与用例引用。
from app.services.app_settings import DISCOVERY_AUTO_DATA_PREP_ENV

AUTO_DATA_PREP_ENV = DISCOVERY_AUTO_DATA_PREP_ENV


def auto_data_prep_enabled(db: Session | None = None) -> bool:
    """发现中心是否允许“由快扫隐式启动数据准备任务”。

    调用方手里有 Session 时请传进来：读到的是“这张库”的设置。
    不传时 `resolve_bool_setting` 会自开一个短会话，并在读失败时退回 env/默认值
    （读一个开关不得把主链路打翻）。
    """
    from app.services.app_settings import (
        DISCOVERY_AUTO_DATA_PREP_KEY,
        resolve_bool_setting,
    )

    return resolve_bool_setting(
        DISCOVERY_AUTO_DATA_PREP_KEY,
        default=True,
        env_var=AUTO_DATA_PREP_ENV,
        db=db,
    )

# 阶段预算（秒）：用于 task_state_machine 判定 stalled
_STAGE_BUDGET_SECONDS = {
    "prepare": 300,        # 配置检查 / dirty 集合预计算
    "market_data": 1800,   # 行情增量同步（A 股 5500 只可能较慢）
    "external_data": 1200, # 财报 / 资金流 / 人气 / 龙虎榜 / 尾盘 / 宏观
    "factor_pipeline": 1800,  # 因子与评分增量计算
    "build_snapshot": 600,    # 写快照主表 + items
    "finalize": 120,          # 收尾统计 / 触发 fast_scan
}

# 每阶段进入时的初始百分比（避免大跳，单步 ≤ 30%）
_STAGE_INITIAL_PERCENT = {
    "prepare": 5,
    "market_data": 15,
    "external_data": 40,
    "factor_pipeline": 55,
    "build_snapshot": 80,
    "finalize": 92,
}

# scope → (region, asset_type) 映射，与 discovery_dirty_set 保持一致
_SCOPE_TO_REGION_ASSET: dict[str, tuple[str, str]] = {
    "cn_stock": ("cn", "stock"),
    "cn-stock": ("cn", "stock"),
    "cn_etf": ("cn", "etf"),
    "cn-etf": ("cn", "etf"),
    "us_stock": ("us", "stock"),
    "us-stock": ("us", "stock"),
    "us_etf": ("us", "etf"),
    "us-etf": ("us", "etf"),
}


def _resolve_scope_config(scope: str) -> tuple[str, str]:
    """解析 scope 为 (region, asset_type)。"""
    if scope not in _SCOPE_TO_REGION_ASSET:
        raise ValueError(f"Unsupported discovery scope: {scope}")
    return _SCOPE_TO_REGION_ASSET[scope]


def _normalize_scope(scope: str) -> str:
    """归一化 scope 为下划线格式（snapshot 表约定）。"""
    return scope.replace("-", "_")


def _is_data_prep_running(db: Session, scope: str) -> AsyncTaskRecord | None:
    """检查某 scope 是否已有 queued/running 状态的 data_prep 任务。

    并发保护：同一 scope 同时只允许一个 data_prep 任务运行（参照 project_memory 硬约束 #3）。
    """
    normalized = _normalize_scope(scope)
    stmt = (
        select(AsyncTaskRecord)
        .where(
            AsyncTaskRecord.task_type == TASK_TYPE_DATA_PREP,
            AsyncTaskRecord.status.in_(("queued", "running")),
        )
        .order_by(desc(AsyncTaskRecord.created_at))
        .limit(20)
    )
    candidates = db.execute(stmt).scalars().all()
    for task in candidates:
        payload = async_tasks._json_loads(task.payload_json, {}) or {}
        task_scope = payload.get("scope")
        if task_scope is None:
            continue
        if _normalize_scope(task_scope) == normalized:
            return task
    return None


def get_latest_data_prep_task(db: Session, scope: str) -> AsyncTaskRecord | None:
    """查询某 scope 最近一个 data_prep 任务（任意状态，供前端轮询状态）。

    Args:
        db: 数据库会话
        scope: 范围

    Returns:
        AsyncTaskRecord 或 None
    """
    normalized = _normalize_scope(scope)
    stmt = (
        select(AsyncTaskRecord)
        .where(AsyncTaskRecord.task_type == TASK_TYPE_DATA_PREP)
        .order_by(AsyncTaskRecord.created_at.desc())
        .limit(50)
    )
    tasks = db.execute(stmt).scalars().all()
    for task in tasks:
        payload = async_tasks._json_loads(task.payload_json, {}) or {}
        task_scope = payload.get("scope")
        if task_scope is None:
            continue
        if _normalize_scope(task_scope) == normalized:
            return task
    return None


def start_data_prep_task(
    *,
    scope: str,
    trade_date: date | None = None,
    force_full_rebuild: bool = False,
    trigger_fast_scan_after_ready: bool = False,
    fast_scan_params: dict | None = None,
) -> AsyncTaskRead:
    """启动后台数据准备任务（异步执行，不受 5 分钟约束）。

    后台数据准备链：
    1. 行情增量同步（调用现有 market_data_sync_task 的增量同步）
    2. 外部因子/宏观更新（调用现有 financial_report_task / macro_update_task /
       hot_rank_task / lhb_institution_task / tail_proxy_task）
    3. 因子与评分增量计算（调用现有 factors/pipeline_task）
    4. 计算 dirty 集合（调用 WP-P.3 compute_dirty_symbols）
    5. 生成 ready 评分快照（写入 DiscoveryScoreSnapshot + Items）
    6. 触发后续操作：可选自动触发快速扫描（trigger_fast_scan_after_ready）

    并发保护：
    - 同一 scope 同时只允许一个 data_prep 任务运行
    - 已有 building 状态快照时跳过快照重建

    Args:
        scope: 范围（cn_stock / cn_etf / us_stock / us_etf，兼容连字符格式）
        trade_date: 指定交易日，None 时使用今天
        force_full_rebuild: 是否强制全量重建快照（忽略 dirty 增量）
        trigger_fast_scan_after_ready: 数据准备就绪后是否自动触发快速扫描
        fast_scan_params: 自动触发快速扫描时使用的参数（透传给 run_fast_scan）

    Returns:
        AsyncTaskRead 任务快照
    """
    # 校验 scope 合法性（即使无 universe 数据也提前校验格式）
    _resolve_scope_config(scope)

    with _PREPARATION_DISPATCH_LOCK:
        SessionLocal = get_session_local()
        db = SessionLocal()
        try:
            existing = _is_data_prep_running(db, scope)
            if existing is not None:
                existing_payload = async_tasks._json_loads(existing.payload_json, {}) or {}
                if trigger_fast_scan_after_ready and not existing_payload.get("trigger_fast_scan_after_ready"):
                    existing_payload["trigger_fast_scan_after_ready"] = True
                    existing_payload["fast_scan_params"] = fast_scan_params or {}
                    existing.payload_json = json.dumps(existing_payload, ensure_ascii=False, default=str)
                    db.commit()
                return async_tasks._task_to_read(existing)
            payload: dict[str, Any] = {
                "scope": scope,
                "trade_date": trade_date.isoformat() if trade_date else None,
                "force_full_rebuild": bool(force_full_rebuild),
                "trigger_fast_scan_after_ready": bool(trigger_fast_scan_after_ready),
                "fast_scan_params": fast_scan_params or {},
            }
            task_read = create_async_task(TASK_TYPE_DATA_PREP, payload, force_new=True)
            _start_worker(task_read.id, _run_data_prep)
            return task_read
        finally:
            db.close()


def _run_data_prep(task_id: str) -> None:
    """后台数据准备 worker 主流程。

    阶段顺序：prepare → market_data → external_data → factor_pipeline →
              build_snapshot → finalize → done

    每阶段：
    - 调用 task_state_machine.enter_stage 进入阶段（重置 stage_started_at / 进度基线）
    - 调用 task_state_machine.update_heartbeat 维护心跳
    - 调用 task_state_machine.update_progress 更新进度（单步 ≤ 30%）

    异常处理：
    - 异常时任务状态变 failed，但不影响已 ready 的旧快照（用户仍可扫描旧数据）
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return
        if task.status in {"cancelled", "failed", "done", "partial"}:
            return

        _ensure_preparation_active(task_id)
        db.refresh(task)
        _set_task(db, task_id, status="running", started_at=task.started_at or _now(),
                  worker_thread_id=hex(get_ident()))

        payload = async_tasks._json_loads(task.payload_json, {}) or {}
        scope = payload.get("scope") or "cn_stock"
        trade_date_str = payload.get("trade_date")
        force_full_rebuild = bool(payload.get("force_full_rebuild", False))
        trigger_fast_scan = bool(payload.get("trigger_fast_scan_after_ready", False))
        fast_scan_params = payload.get("fast_scan_params") or {}

        trade_date: date | None
        if trade_date_str:
            try:
                trade_date = date.fromisoformat(trade_date_str)
            except (ValueError, TypeError):
                trade_date = None
        else:
            trade_date = None

        # ── 阶段 1：prepare ───────────────────────────────────
        enter_stage(
            task_id,
            "prepare",
            stage_budget_seconds=_STAGE_BUDGET_SECONDS["prepare"],
            db=db,
        )
        update_progress(
            task_id,
            percent=_STAGE_INITIAL_PERCENT["prepare"],
            current_step_description="检查配置与 dirty 集合",
            db=db,
        )
        update_heartbeat(task_id, current_step_description="prepare: 配置检查", db=db)

        normalized_scope = _normalize_scope(scope)
        # 查询当前 ready 快照（用于 dirty 集合计算与全量重建判断）
        current_snapshot = _get_ready_snapshot_internal(db, normalized_scope)
        # Recover interrupted historical builds; never alter an active source.
        _recover_abandoned_snapshots(db, normalized_scope)
        existing_building = is_snapshot_building_for_scope(db, normalized_scope)

        trigger_full, reason = should_trigger_full_rebuild(
            db,
            scope=normalized_scope,
            current_snapshot=current_snapshot,
        )
        if force_full_rebuild:
            trigger_full = True
            reason = "force_full_rebuild"

        # 计算 dirty 集合（用于 build_snapshot 阶段决定需要重算哪些标的）
        dirty_symbols = compute_dirty_symbols(
            db, scope=normalized_scope, current_snapshot=current_snapshot,
        )

        update_progress(
            task_id,
            percent=min(_STAGE_INITIAL_PERCENT["prepare"] + 5, 30),
            current_step_description=f"prepare 完成：dirty={len(dirty_symbols)} full={trigger_full}",
            db=db,
        )

        # ── 阶段 2：market_data 行情增量同步 ─────────────────
        enter_stage(
            task_id,
            "market_data",
            stage_budget_seconds=_STAGE_BUDGET_SECONDS["market_data"],
            db=db,
        )
        update_heartbeat(
            task_id,
            current_step_description="market_data: 行情增量同步",
            db=db,
        )
        try:
            db.rollback()
            child = _invoke_market_data_sync(scope)
            market_state = _wait_for_preparation_child(task_id, child, "market_data", db)
            from app.services.factors.__facade__ import get_active_runtime_with_fallback_reason
            runtime = get_active_runtime_with_fallback_reason(db=db)
            market_result = (market_state or {}).get("result") or {}
            market_payload = async_tasks._json_loads((market_state or {}).get("payload_json"), {}) or {}
            source_symbol_ids = market_payload.get("symbol_ids") or []
            if runtime.weight_mode == "manual" and (
                not source_symbol_ids
                or market_result.get("symbols_total") != len(source_symbol_ids)
                or market_result.get("scored_count") != len(source_symbol_ids)
            ):
                raise RuntimeError("行情同步完成但本轮评分未全部提交，未发布新快照；请检查评分或交易计划错误后重试")
        except _PreparationCancelled:
            raise
        except Exception as exc:
            db.rollback()
            logger.warning("data_prep %s market_data sync failed: %s", task_id, exc, exc_info=False)
            _append_error(db.get(AsyncTaskRecord, task_id) or task, {
                "stage": "market_data",
                "error": str(exc),
            })
            db.commit()
            raise
        update_progress(
            task_id,
            percent=_STAGE_INITIAL_PERCENT["market_data"],
            current_step_description="market_data 完成",
            db=db,
        )

        # ── 阶段 3：external_data 外部因子/宏观更新 ──────────
        enter_stage(
            task_id,
            "external_data",
            stage_budget_seconds=_STAGE_BUDGET_SECONDS["external_data"],
            db=db,
        )
        update_heartbeat(
            task_id,
            current_step_description="external_data: 财报/资金流/人气/龙虎榜/尾盘/宏观",
            db=db,
        )
        external_errors = _invoke_external_data_sync(task_id=task_id, db=db)
        if external_errors:
            task_obj = db.get(AsyncTaskRecord, task_id)
            if task_obj is not None:
                for err in external_errors:
                    _append_error(task_obj, err)
                db.commit()
        update_progress(
            task_id,
            percent=_STAGE_INITIAL_PERCENT["external_data"],
            current_step_description="external_data 完成",
            db=db,
        )

        # ── 阶段 4：factor_pipeline 因子与评分增量计算 ────────
        enter_stage(
            task_id,
            "factor_pipeline",
            stage_budget_seconds=_STAGE_BUDGET_SECONDS["factor_pipeline"],
            db=db,
        )
        update_heartbeat(
            task_id,
            current_step_description="factor_pipeline: 因子与评分增量计算",
            db=db,
        )
        try:
            db.rollback()
            child = _invoke_factor_pipeline()
            factor_state = _wait_for_preparation_child(task_id, child, "factor_pipeline", db)
        except _PreparationCancelled:
            raise
        except Exception as exc:
            db.rollback()
            logger.warning("data_prep %s factor_pipeline failed: %s", task_id, exc, exc_info=False)
            task_obj = db.get(AsyncTaskRecord, task_id)
            if task_obj is not None:
                _append_error(task_obj, {
                    "stage": "factor_pipeline",
                    "error": str(exc),
                })
                db.commit()
            raise
        update_progress(
            task_id,
            percent=_STAGE_INITIAL_PERCENT["factor_pipeline"],
            current_step_description="factor_pipeline 完成",
            db=db,
        )

        # ── 阶段 5：build_snapshot 生成 ready 评分快照 ────────
        enter_stage(
            task_id,
            "build_snapshot",
            stage_budget_seconds=_STAGE_BUDGET_SECONDS["build_snapshot"],
            db=db,
        )
        update_heartbeat(
            task_id,
            current_step_description="build_snapshot: 写入 DiscoveryScoreSnapshot + Items",
            db=db,
        )

        snapshot_id: int | None = None
        _ensure_preparation_active(task_id)
        current_runtime = get_active_runtime_with_fallback_reason(db=db)
        if (current_runtime.weight_mode, current_runtime.active_model_id) != (runtime.weight_mode, runtime.active_model_id):
            raise RuntimeError("准备期间评分模式或模型已变化，请重新准备，未发布新快照")
        score_batch_ids = None
        if runtime.weight_mode != "manual":
            bridge = ((factor_state or {}).get("result") or {}).get("scoring_bridge") or {}
            score_batch_ids = [batch["calc_batch_id"] for batch in bridge.get("daily_batches", [])
                               if batch.get("materialized_count") and batch.get("calc_batch_id")]
            if not bridge.get("materialized_count") or not score_batch_ids:
                raise RuntimeError("本轮因子评分未实际发布，未复用旧评分生成新快照")
        score_dates = _prepared_score_dates(db, source_symbol_ids, score_batch_ids)
        source_date_min, source_date_max = min(score_dates.values()), max(score_dates.values())
        source_region, _ = _resolve_scope_config(scope)
        source_tz = ZoneInfo("Asia/Shanghai" if source_region == "cn" else "America/New_York")
        source_close_at = datetime.combine(source_date_max, clock_time(15 if source_region == "cn" else 16),
            tzinfo=source_tz).astimezone(timezone.utc).replace(tzinfo=None)
        source_observed_at = _now()
        source_data_status = "intraday" if source_close_at > source_observed_at else "closed"
        if trade_date is not None and source_date_max > trade_date:
            raise RuntimeError("本轮评分晚于请求的历史交易日，不能用当前同步生成该历史快照")
        if existing_building is not None:
            # 已有 building 快照在跑（理论不应发生：start_data_prep_task 并发保护应拦截），
            # 这里安全降级：不重建，记录 degraded_reason，让用户复用旧 ready 快照
            logger.warning(
                "data_prep %s: snapshot building already exists (id=%s), skip rebuild",
                task_id, existing_building.id,
            )
            raise RuntimeError("该范围存在尚未就绪的评分快照，请先在任务中心处理构建任务")
        else:
            try:
                snapshot_id = _build_ready_snapshot(
                    db,
                    scope=normalized_scope,
                    trade_date=source_date_max,
                    trigger_full_rebuild=trigger_full,
                    full_rebuild_reason=reason,
                    dirty_symbols=dirty_symbols,
                    source_task_id=task_id,
                    source_symbol_ids=source_symbol_ids,
                    score_batch_ids=score_batch_ids,
                    source_observed_at=source_observed_at,
                )
            except Exception as exc:
                logger.exception("data_prep %s build_snapshot failed: %s", task_id, exc)
                db.rollback()
                task_obj = db.get(AsyncTaskRecord, task_id)
                if task_obj is not None:
                    _append_error(task_obj, {
                        "stage": "build_snapshot",
                        "error": str(exc),
                        "traceback": traceback.format_exc(limit=6),
                    })
                    db.commit()
                raise

        update_progress(
            task_id,
            percent=_STAGE_INITIAL_PERCENT["build_snapshot"],
            current_step_description=f"build_snapshot 完成 snapshot_id={snapshot_id}",
            db=db,
        )

        # ── 阶段 6：finalize 收尾统计 ─────────────────────────
        enter_stage(
            task_id,
            "finalize",
            stage_budget_seconds=_STAGE_BUDGET_SECONDS["finalize"],
            db=db,
        )
        update_heartbeat(
            task_id,
            current_step_description="finalize: 写入统计与触发后续操作",
            db=db,
        )

        task_obj = db.get(AsyncTaskRecord, task_id)
        result_summary = async_tasks._json_loads(task_obj.result_json if task_obj else None, {}) or {}
        result_summary.update({
            "scope": normalized_scope,
            "snapshot_id": snapshot_id,
            "dirty_symbol_count": len(dirty_symbols),
            "full_rebuild_triggered": trigger_full,
            "full_rebuild_reason": reason,
            "fast_scan": None,
            "score_date_min": source_date_min.isoformat(),
            "score_date_max": source_date_max.isoformat(),
            "data_cutoff_at": db.get(DiscoveryScoreSnapshot, snapshot_id).data_cutoff_at,
            "source_observed_at": source_observed_at,
            "source_data_status": source_data_status,
        })
        _set_task(
            db,
            task_id,
            result_json=json.dumps(result_summary, ensure_ascii=False, default=str),
        )

        # Serialize only the final intent read / no-scan terminal transition
        # with dispatch, so a coalesced request can still ask for an auto scan.
        with _PREPARATION_DISPATCH_LOCK:
            _ensure_preparation_active(task_id)
            db.refresh(task_obj)
            latest_payload = async_tasks._json_loads(task_obj.payload_json, {}) or {}
            trigger_fast_scan = bool(latest_payload.get("trigger_fast_scan_after_ready"))
            fast_scan_params = latest_payload.get("fast_scan_params") or {}
            if not trigger_fast_scan:
                _complete_preparation(db, task_id, scanned=False)
                return
            _set_task(db, task_id, stage="fast_scan", message="评分快照已就绪，正在自动扫描")
        db.rollback()
        if trigger_fast_scan:
            try:
                scan_result = _trigger_fast_scan_after_ready(scope, fast_scan_params, task_id=task_id)
            except Exception as exc:
                result_summary["fast_scan"] = {
                    "status": "failed", "snapshot_id": snapshot_id, "scan_run_id": None,
                    "result_rows_written": 0, "cache_hit": False, "degraded_reason": str(exc),
                    "recommended_action": "评分快照已保留，请检查扫描参数后重试",
                }
                _set_task(db, task_id, result_json=json.dumps(result_summary, ensure_ascii=False, default=str))
                raise
            result_summary["fast_scan"] = scan_result
            _set_task(db, task_id, result_json=json.dumps(result_summary, ensure_ascii=False, default=str))
            _ensure_preparation_active(task_id)
            if scan_result.get("status") != "ok":
                raise RuntimeError(f"快速扫描未完成：{scan_result.get('degraded_reason') or scan_result.get('status')}")
        _complete_preparation(db, task_id, scanned=True)

    except _PreparationCancelled:
        db.rollback()
        _fail_unpublished_snapshots(db, task_id, "数据准备已取消，未发布此版本")
        _set_task(db, task_id, status="cancelled", stage="cancelled",
                  message="数据准备已取消", finished_at=_now())
    except Exception as exc:
        logger.exception("data_prep %s worker crashed: %s", task_id, exc)
        try:
            db.rollback()
            _fail_unpublished_snapshots(db, task_id, str(exc))
            task_obj = db.get(AsyncTaskRecord, task_id)
            if task_obj is not None and task_obj.status not in ("done", "failed", "cancelled"):
                _append_error(task_obj, {
                    "scope": "task",
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                })
                _set_task(
                    db,
                    task_id,
                    status="failed",
                    stage="failed",
                    percent=100,
                    message="数据准备失败（不影响已 ready 的旧快照）",
                    finished_at=_now(),
                )
        except Exception:
            logger.exception("data_prep %s failed to mark as failed", task_id)
    finally:
        db.close()


def _get_ready_snapshot_internal(
    db: Session, scope: str
) -> DiscoveryScoreSnapshot | None:
    """内部使用：查询某 scope 当前 ready 快照。

    与 discovery_fast_scan.get_ready_snapshot 保持一致语义，但直接操作传入的 db，
    避免在 worker 中创建额外 session。
    """
    normalized = _normalize_scope(scope)
    stmt = (
        select(DiscoveryScoreSnapshot)
        .where(
            DiscoveryScoreSnapshot.scope == normalized,
            DiscoveryScoreSnapshot.status == "ready",
        )
        .order_by(DiscoveryScoreSnapshot.generated_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def _build_ready_snapshot(
    db: Session,
    *,
    scope: str,
    trade_date: date,
    trigger_full_rebuild: bool,
    full_rebuild_reason: str | None,
    dirty_symbols: list,
    source_task_id: str,
    source_symbol_ids: list[int] | None = None,
    score_batch_ids: list[str] | None = None,
    source_observed_at: datetime | None = None,
) -> int | None:
    """生成 ready 评分快照（building → ready，原子切换）。

    流程：
    1. 创建 DiscoveryScoreSnapshot(status=building)
    2. 从 Score 表读取最新评分，写入 DiscoveryScoreSnapshotItem
    3. 同事务将 status: building → ready，写入 generated_at / build_duration
    4. 若有旧 ready 快照 → mark_snapshot_superseded

    关键数据操作必须 commit 后再继续（参照 project_memory 硬约束 #2）。

    返回 snapshot_id；若失败返回 None。
    """
    from sqlalchemy import func as sa_func

    normalized = _normalize_scope(scope)
    region, asset_type = _resolve_scope_config(scope)
    # Older direct builders used a descriptive source label rather than an
    # AsyncTaskRecord. Actual preparation parents always have a persisted task.
    has_parent_task = db.get(AsyncTaskRecord, source_task_id) is not None

    # 创建 building 快照
    now = _now()
    snap = DiscoveryScoreSnapshot(
        scope=normalized,
        trade_date=datetime.combine(trade_date, datetime.min.time()),
        status="building",
        source_task_id=source_task_id,
    )
    db.add(snap)
    db.flush()  # 获取 snap.id
    snapshot_id = snap.id

    # 加载该 scope 全部 universe_symbols：code → universe_symbol_id
    us_rows = db.execute(
        select(UniverseSymbol.id, UniverseSymbol.symbol).where(
            UniverseSymbol.region == region,
            UniverseSymbol.asset_type == asset_type,
        )
    ).all()
    code_to_us_id: dict[str, int] = {str(row.symbol): int(row.id) for row in us_rows}

    if code_to_us_id:
        # 找到 symbols 表对应的 symbol_id（按 code 批量查）
        symbol_statement = select(Symbol.id, Symbol.symbol).where(Symbol.symbol.in_(list(code_to_us_id.keys())))
        if source_symbol_ids is not None:
            symbol_statement = symbol_statement.where(Symbol.id.in_(source_symbol_ids))
        symbol_rows = db.execute(symbol_statement).all()
        # code → symbol_id（业务表）
        code_to_symbol_id: dict[str, int] = {
            str(row.symbol): int(row.id) for row in symbol_rows
        }
        symbol_ids = list(code_to_symbol_id.values())

        if symbol_ids:
            # 读取每个 symbol 的最新 Score（按 symbol_id 分组取最新 trade_date）
            from app.services.factors.__facade__ import apply_active_score_scope_to_scores_select
            latest_score_statement = (
                select(
                    Score.symbol_id,
                    sa_func.max(Score.trade_date).label("max_date"),
                )
                .where(Score.symbol_id.in_(symbol_ids), Score.trade_date <= trade_date)
                .group_by(Score.symbol_id)
            )
            if score_batch_ids is not None:
                latest_score_statement = latest_score_statement.where(Score.calc_batch_id.in_(score_batch_ids))
            latest_score_subq = apply_active_score_scope_to_scores_select(latest_score_statement, db).subquery()

            score_statement = select(Score).join(
                    latest_score_subq,
                    (Score.symbol_id == latest_score_subq.c.symbol_id)
                    & (Score.trade_date == latest_score_subq.c.max_date),
                ).order_by(Score.id.desc())
            if score_batch_ids is not None:
                score_statement = score_statement.where(Score.calc_batch_id.in_(score_batch_ids))
            score_rows = db.execute(apply_active_score_scope_to_scores_select(score_statement, db)).scalars().all()

            # 反向映射：symbol_id → code（用于查找 universe_symbol_id）
            symbol_id_to_code = {sid: code for code, sid in code_to_symbol_id.items()}

            seen_symbols = set()
            selected_score_dates = {}
            for score in score_rows:
                if score.symbol_id in seen_symbols:
                    continue
                seen_symbols.add(score.symbol_id)
                selected_score_dates[score.symbol_id] = score.trade_date
                code = symbol_id_to_code.get(score.symbol_id)
                if code is None:
                    continue
                us_id = code_to_us_id.get(code)
                if us_id is None:
                    continue

                item = DiscoveryScoreSnapshotItem(
                    snapshot_id=snapshot_id,
                    universe_symbol_id=us_id,
                    symbol_id=score.symbol_id,
                    quality_score=score.quality_score,
                    timing_score=score.timing_score,
                    priority_score=score.priority_score,
                    dimension_scores_json=score.dimension_scores_json,
                    stage=score.stage,
                    action=score.action,
                    data_credibility=score.data_credibility,
                )
                db.add(item)

    # Session factories disable autoflush; count only after the items exist.
    db.flush()
    item_count = int(
        db.execute(
            select(sa_func.count(DiscoveryScoreSnapshotItem.id)).where(
                DiscoveryScoreSnapshotItem.snapshot_id == snapshot_id
            )
        ).scalar_one()
        or 0
    )
    snap.symbol_count = item_count
    snap.dirty_symbol_count = len(dirty_symbols)
    snap.coverage_pct = round(item_count / max(len(code_to_us_id), 1) * 100, 2)
    if item_count == 0:
        raise RuntimeError("没有可用评分，未发布空快照；请检查行情与评分任务")
    if source_symbol_ids is not None:
        if item_count != len(source_symbol_ids):
            raise RuntimeError("本轮已准备标的缺少对应基础池或评分，未发布不完整快照")
        snap.trade_date = datetime.combine(max(selected_score_dates.values()), datetime.min.time())
        market_tz = ZoneInfo("Asia/Shanghai" if region == "cn" else "America/New_York")
        source_close_at = datetime.combine(min(selected_score_dates.values()),
            clock_time(15 if region == "cn" else 16), tzinfo=market_tz).astimezone(timezone.utc).replace(tzinfo=None)
        snap.data_cutoff_at = min(source_close_at, source_observed_at or _now())

    # 关键数据操作：先 commit items（避免大事务），再切换状态
    db.commit()

    # A cancellation received while writing items must never replace the old ready version.
    if has_parent_task:
        _ensure_preparation_active(source_task_id)
    if source_symbol_ids is not None and _prepared_score_dates(db, source_symbol_ids, score_batch_ids) != selected_score_dates:
        raise RuntimeError("发布前行情或评分日期已变化，未替换旧快照，请重试")
    # Replace the current version atomically; old history remains addressable.
    old_ready = _get_ready_snapshot_internal_excluding(db, normalized, snapshot_id)
    if old_ready is not None:
        mark_snapshot_superseded(db, old_ready.id)
    snap.status = "ready"
    snap.generated_at = _now()
    snap.build_duration_seconds = (_now() - now).total_seconds()
    db.commit()
    return snapshot_id


def _get_ready_snapshot_internal_excluding(
    db: Session, scope: str, exclude_snapshot_id: int
) -> DiscoveryScoreSnapshot | None:
    """查找旧 ready 快照（排除刚创建的那个）。"""
    normalized = _normalize_scope(scope)
    stmt = (
        select(DiscoveryScoreSnapshot)
        .where(
            DiscoveryScoreSnapshot.scope == normalized,
            DiscoveryScoreSnapshot.status == "ready",
            DiscoveryScoreSnapshot.id != exclude_snapshot_id,
        )
        .order_by(DiscoveryScoreSnapshot.generated_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def _invoke_market_data_sync(scope: str):
    """调用现有 market_data_sync_task 的增量同步（针对该 scope 标的）。

    复用现有稳定逻辑，不重写。失败仅记录日志，不阻塞后续阶段。
    """
    from app.services.market_data_sync_task import create_market_data_sync_task
    from app.schemas.async_task import MarketDataSyncCreate
    from app.services.regions import markets_for_region

    region, asset_type = _resolve_scope_config(scope)
    # Freeze the requested region and asset into the existing explicit-symbol
    # contract; asset_types alone would also fetch the other region's symbols.
    with get_session_local()() as db:
        symbol_ids = list(db.scalars(select(Symbol.id).where(
            Symbol.is_active == 1,
            Symbol.asset_type == asset_type,
            Symbol.market.in_(markets_for_region(region)),
        ).order_by(Symbol.id)).all())
    if not symbol_ids:
        raise ValueError(f"{scope}: 没有可同步的活跃标的")
    payload = MarketDataSyncCreate(
        scope="symbols",
        symbol_ids=symbol_ids,
        asset_types=[asset_type],
        auto_scan=False,  # 不在数据准备阶段触发扫描
    )
    child = create_market_data_sync_task(payload)
    child_payload = async_tasks._json_loads(child.get("payload_json"), {}) or {}
    if (child_payload.get("scope") != "symbols"
            or set(child_payload.get("symbol_ids") or []) != set(symbol_ids)
            or child_payload.get("asset_types") != [asset_type]
            or child_payload.get("auto_scan") is not False):
        raise RuntimeError(f"已有其他范围行情同步任务 {child.get('id')}，请等待其完成后重试本范围准备；原任务未取消")
    return child


def _invoke_external_data_sync(*, task_id: str | None = None, db: Session | None = None) -> list[dict]:
    """调用现有外部数据同步任务（财报/资金流/人气/龙虎榜/宏观）。

    每个任务独立调用，失败仅记录错误，不影响其他任务。
    返回错误列表，由调用方追加到 task.errors_json。
    """
    errors: list[dict] = []
    children = []
    if db is not None:
        db.rollback()

    # 1. 财报同步
    try:
        from app.services.financial_report_task import create_financial_report_task
        children.append(("external_data.financial_report", create_financial_report_task(source="all", limit=100)))
    except Exception as exc:
        errors.append({"stage": "external_data.financial_report", "error": str(exc)})

    # 2. 宏观数据更新
    try:
        from app.services.macro_update_task import create_macro_update_task
        from app.schemas.macro import MacroUpdateRequest
        children.append(("external_data.macro_update", create_macro_update_task(MacroUpdateRequest(region="all"))))
    except Exception as exc:
        errors.append({"stage": "external_data.macro_update", "error": str(exc)})

    # 3. 人气榜快照
    try:
        from app.services.hot_rank_task import create_hot_rank_task
        children.append(("external_data.hot_rank", create_hot_rank_task()))
    except Exception as exc:
        errors.append({"stage": "external_data.hot_rank", "error": str(exc)})

    # 4. 龙虎榜机构席位同步
    try:
        from app.services.lhb_institution_task import create_lhb_institution_task
        children.append(("external_data.lhb_institution", create_lhb_institution_task(lookback_days=3)))
    except Exception as exc:
        errors.append({"stage": "external_data.lhb_institution", "error": str(exc)})

    if task_id and db is not None:
        for stage, child in children:
            try:
                _wait_for_preparation_child(task_id, child, stage, db)
            except _PreparationCancelled:
                raise
            except Exception as exc:
                db.rollback()
                errors.append({"stage": stage, "error": str(exc)})
    return errors


def _invoke_factor_pipeline():
    """调用因子域 Facade 的增量评分流水线（防腐层：外部域禁止直接 import pipeline_task）。

    复用现有稳定逻辑，不重写。失败仅记录日志。
    """
    # near-relative coupling: discovery 域只能走 scoring Facade；audit 2026-08-30
    from app.services.factors.__facade__ import create_scoring_task, get_active_runtime_with_fallback_reason
    if get_active_runtime_with_fallback_reason().weight_mode == "manual":
        # The preceding market sync already calculates traditional manual Scores.
        return None

    return create_scoring_task(
        scope="recent",
        full_refresh=False,
        train_model=False,
        materialize_scores=True,
        actor="internal:discovery_data_prep",
        source_hint="discovery_data_prep",
    )


def _trigger_fast_scan_after_ready(scope: str, fast_scan_params: dict, *, task_id: str | None = None) -> dict:
    """数据准备就绪后自动触发快速扫描。

    在独立 session 中执行，避免影响 data_prep 任务的 session 生命周期。
    """
    from app.services.discovery_fast_scan import run_fast_scan

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        params = {
            "scope": scope,
            "min_score": 55,
            "limit": 300,
        }
        params.update(fast_scan_params or {})
        params["scope"] = scope
        if task_id:
            params["cancel_token"] = _PreparationScanCancelToken(task_id=task_id)
        result = run_fast_scan(db=db, **params)
        from app.models.scan import ScanRun
        scan_run_id = result.get("cached_from_scan_run_id")
        if scan_run_id is None and result.get("cache_key"):
            scan_run_id = db.scalar(select(ScanRun.id).where(
                ScanRun.cache_key == result["cache_key"], ScanRun.snapshot_id == result.get("snapshot_id"),
            ).order_by(ScanRun.id.desc()).limit(1))
        return {"status": result.get("status") or "failed", "snapshot_id": result.get("snapshot_id"),
                "scan_run_id": scan_run_id, "result_rows_written": int(result.get("result_rows_written") or 0),
                "cache_hit": bool(result.get("cache_hit")), "degraded_reason": result.get("degraded_reason"),
                "recommended_action": result.get("recommended_action")}
    finally:
        db.close()


__all__ = [
    "TASK_TYPE_DATA_PREP",
    "start_data_prep_task",
    "_run_data_prep",
]
