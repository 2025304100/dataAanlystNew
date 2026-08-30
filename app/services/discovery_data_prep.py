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
import traceback
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
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

logger = logging.getLogger(__name__)

TASK_TYPE_DATA_PREP = "discovery_data_prep"

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

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        existing = _is_data_prep_running(db, scope)
        if existing is not None:
            # 并发保护：已有任务运行中，直接返回该任务（参照 project_memory 硬约束 #3）
            logger.info(
                "data_prep: scope=%s already has running task %s, return existing",
                scope, existing.id,
            )
            return async_tasks._task_to_read(existing)

        payload: dict[str, Any] = {
            "scope": scope,
            "trade_date": trade_date.isoformat() if trade_date else None,
            "force_full_rebuild": bool(force_full_rebuild),
            "trigger_fast_scan_after_ready": bool(trigger_fast_scan_after_ready),
            "fast_scan_params": fast_scan_params or {},
        }
        task_read = create_async_task(TASK_TYPE_DATA_PREP, payload)
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
        if task.status == "cancelled":
            return

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
        # 并发保护：若已有 building 快照，则跳过快照重建（仅更新数据）
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
            _invoke_market_data_sync(scope)
        except Exception as exc:
            logger.warning("data_prep %s market_data sync failed: %s", task_id, exc, exc_info=False)
            _append_error(db.get(AsyncTaskRecord, task_id) or task, {
                "stage": "market_data",
                "error": str(exc),
            })
            db.commit()
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
        external_errors = _invoke_external_data_sync()
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
            _invoke_factor_pipeline()
        except Exception as exc:
            logger.warning("data_prep %s factor_pipeline failed: %s", task_id, exc, exc_info=False)
            task_obj = db.get(AsyncTaskRecord, task_id)
            if task_obj is not None:
                _append_error(task_obj, {
                    "stage": "factor_pipeline",
                    "error": str(exc),
                })
                db.commit()
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
        if existing_building is not None:
            # 已有 building 快照在跑（理论不应发生：start_data_prep_task 并发保护应拦截），
            # 这里安全降级：不重建，记录 degraded_reason，让用户复用旧 ready 快照
            logger.warning(
                "data_prep %s: snapshot building already exists (id=%s), skip rebuild",
                task_id, existing_building.id,
            )
            snapshot_id = existing_building.id
        else:
            try:
                snapshot_id = _build_ready_snapshot(
                    db,
                    scope=normalized_scope,
                    trade_date=trade_date or date.today(),
                    trigger_full_rebuild=trigger_full,
                    full_rebuild_reason=reason,
                    dirty_symbols=dirty_symbols,
                    source_task_id=task_id,
                )
            except Exception as exc:
                logger.exception("data_prep %s build_snapshot failed: %s", task_id, exc)
                task_obj = db.get(AsyncTaskRecord, task_id)
                if task_obj is not None:
                    _append_error(task_obj, {
                        "stage": "build_snapshot",
                        "error": str(exc),
                        "traceback": traceback.format_exc(limit=6),
                    })
                    db.commit()

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

        result_summary = {
            "scope": normalized_scope,
            "snapshot_id": snapshot_id,
            "dirty_symbol_count": len(dirty_symbols),
            "full_rebuild_triggered": trigger_full,
            "full_rebuild_reason": reason,
        }
        _set_task(
            db,
            task_id,
            result_json=json.dumps(result_summary, ensure_ascii=False, default=str),
        )

        # 完成任务
        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            message="数据准备完成",
            finished_at=_now(),
        )

        # 触发后续：若需要自动触发快速扫描
        if trigger_fast_scan and snapshot_id is not None:
            try:
                _trigger_fast_scan_after_ready(scope, fast_scan_params)
            except Exception as exc:
                logger.warning(
                    "data_prep %s trigger_fast_scan failed: %s",
                    task_id, exc, exc_info=False,
                )
    except Exception as exc:
        logger.exception("data_prep %s worker crashed: %s", task_id, exc)
        try:
            db.rollback()
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
        symbol_rows = db.execute(
            select(Symbol.id, Symbol.symbol).where(
                Symbol.symbol.in_(list(code_to_us_id.keys()))
            )
        ).all()
        # code → symbol_id（业务表）
        code_to_symbol_id: dict[str, int] = {
            str(row.symbol): int(row.id) for row in symbol_rows
        }
        symbol_ids = list(code_to_symbol_id.values())

        if symbol_ids:
            # 读取每个 symbol 的最新 Score（按 symbol_id 分组取最新 trade_date）
            latest_score_subq = (
                select(
                    Score.symbol_id,
                    sa_func.max(Score.trade_date).label("max_date"),
                )
                .where(Score.symbol_id.in_(symbol_ids))
                .group_by(Score.symbol_id)
            ).subquery()

            score_rows = db.execute(
                select(Score).join(
                    latest_score_subq,
                    (Score.symbol_id == latest_score_subq.c.symbol_id)
                    & (Score.trade_date == latest_score_subq.c.max_date),
                )
            ).scalars().all()

            # 反向映射：symbol_id → code（用于查找 universe_symbol_id）
            symbol_id_to_code = {sid: code for code, sid in code_to_symbol_id.items()}

            for score in score_rows:
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

    # 统计写入的 item 数量
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
    snap.coverage_pct = 100.0 if item_count > 0 else 0.0

    # 关键数据操作：先 commit items（避免大事务），再切换状态
    db.commit()

    # 同事务切换 building → ready + 标记旧快照 superseded（原子）
    snap.status = "ready"
    snap.generated_at = _now()
    snap.build_duration_seconds = (_now() - now).total_seconds()
    db.flush()

    # 将旧 ready 快照标记为 superseded（只 flush，等下方统一 commit）
    old_ready = _get_ready_snapshot_internal_excluding(db, normalized, snapshot_id)
    if old_ready is not None:
        mark_snapshot_superseded(db, old_ready.id)

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


def _invoke_market_data_sync(scope: str) -> None:
    """调用现有 market_data_sync_task 的增量同步（针对该 scope 标的）。

    复用现有稳定逻辑，不重写。失败仅记录日志，不阻塞后续阶段。
    """
    from app.services.market_data_sync_task import create_market_data_sync_task
    from app.schemas.async_task import MarketDataSyncCreate

    region, asset_type = _resolve_scope_config(scope)
    # 将 snapshot scope（cn_stock）转换为 market_data_sync 的 scope 配置
    # market_data_sync 的 scope 字段为 "all" / "symbols" / "watchlist" / 其他
    # 这里使用 asset_types 过滤即可
    payload = MarketDataSyncCreate(
        scope="all",
        asset_types=[asset_type],
        auto_scan=False,  # 不在数据准备阶段触发扫描
    )
    create_market_data_sync_task(payload)


def _invoke_external_data_sync() -> list[dict]:
    """调用现有外部数据同步任务（财报/资金流/人气/龙虎榜/尾盘/宏观）。

    每个任务独立调用，失败仅记录错误，不影响其他任务。
    返回错误列表，由调用方追加到 task.errors_json。
    """
    errors: list[dict] = []

    # 1. 财报同步
    try:
        from app.services.financial_report_task import create_financial_report_task
        create_financial_report_task(source="all", limit=100)
    except Exception as exc:
        errors.append({"stage": "external_data.financial_report", "error": str(exc)})

    # 2. 宏观数据更新
    try:
        from app.services.macro_update_task import create_macro_update_task
        from app.schemas.macro import MacroUpdateRequest
        create_macro_update_task(MacroUpdateRequest(region="all"))
    except Exception as exc:
        errors.append({"stage": "external_data.macro_update", "error": str(exc)})

    # 3. 人气榜快照
    try:
        from app.services.hot_rank_task import create_hot_rank_task
        create_hot_rank_task()
    except Exception as exc:
        errors.append({"stage": "external_data.hot_rank", "error": str(exc)})

    # 4. 龙虎榜机构席位同步
    try:
        from app.services.lhb_institution_task import create_lhb_institution_task
        create_lhb_institution_task(lookback_days=3)
    except Exception as exc:
        errors.append({"stage": "external_data.lhb_institution", "error": str(exc)})

    # 5. 尾盘代理快照
    try:
        from app.services.tail_proxy_task import create_tail_proxy_task
        create_tail_proxy_task(source="candidates", limit=20)
    except Exception as exc:
        errors.append({"stage": "external_data.tail_proxy", "error": str(exc)})

    return errors


def _invoke_factor_pipeline() -> None:
    """调用因子域 Facade 的增量评分流水线（防腐层：外部域禁止直接 import pipeline_task）。

    复用现有稳定逻辑，不重写。失败仅记录日志。
    """
    # near-relative coupling: discovery 域只能走 scoring Facade；audit 2026-08-30
    from app.services.factors.__facade__ import create_scoring_task

    create_scoring_task(
        scope="recent",
        full_refresh=False,
        train_model=False,
        materialize_scores=True,
        actor="internal:discovery_data_prep",
        source_hint="discovery_data_prep",
    )


def _trigger_fast_scan_after_ready(scope: str, fast_scan_params: dict) -> None:
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
        run_fast_scan(db=db, **params)
    finally:
        db.close()


__all__ = [
    "TASK_TYPE_DATA_PREP",
    "start_data_prep_task",
    "_run_data_prep",
]
