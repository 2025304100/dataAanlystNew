import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import AsyncTaskRead
from app.schemas.discovery import (
    DataPrepRequest,
    DiscoveryIndicatorEvaluateRequest,
    DiscoveryIndicatorEvaluationRow,
    DiscoveryResultUpdate,
    DiscoveryScopeStatsRead,
    DiscoveryTaskCreate,
    DiscoveryTaskRead,
    FastScanRequest,
    FastScanResponse,
    SnapshotStatusRead,
)
from app.services.discovery_tasks import (
    cancel_discovery_task,
    create_discovery_task,
    get_discovery_scope_stats,
    get_discovery_task,
    list_discovery_tasks,
    pause_discovery_task,
    resume_discovery_task,
    retry_discovery_task,
)
from app.services.discovery_results import evaluate_discovery_indicators, get_latest_discovery_candidates, update_discovery_result, update_discovery_symbol
from app.services.discovery_cleanup import cleanup_expired_discovery_results
from app.services.candidate_promote import (
    get_latest_scan_run_candidates,
    list_candidates,
    promote_candidate,
    promote_candidates_batch,
    unpromote_candidate,
)
from app.services.discovery_data_prep import (
    TASK_TYPE_DATA_PREP,
    start_data_prep_task,
)
from app.services.discovery_dirty_set import is_snapshot_building_for_scope
from app.services.discovery_history import (
    get_scan_run_detail,
    list_excluded_candidates,
    list_scan_runs,
)
from app.services.opportunity_transitions import (
    exclude_candidate as exclude_candidate_service,
    restore_candidate as restore_candidate_service,
    transition_candidate_to_observation,
)
from app.services.discovery_fast_scan import (
    TASK_TYPE_FAST_SCAN,
    get_ready_snapshot,
    run_fast_scan,
)


def _normalize_scope_for_snapshot(scope: str) -> str:
    """scope 归一化（snapshot 表用下划线格式）。"""
    return scope.replace("-", "_")


router = APIRouter()


@router.post("/discovery/tasks", response_model=DiscoveryTaskRead)
def create_task(payload: DiscoveryTaskCreate):
    return create_discovery_task(payload)


@router.get("/discovery/latest-candidates")
def get_latest_candidates(
    min_score: float = Query(default=0.0, ge=0.0, le=100.0),
    limit: int = Query(default=50, ge=1, le=200),
    scope: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """获取最新一次挖掘任务的全量候选（不依赖 executable 过滤）。

    P1 改造：优先从 discovery_candidates 表读取（带 is_promoted 字段），
    若最新任务为 P1 改造前的历史任务（无候选记录），回退查 ScanResult 兼容。

    挖掘结果展示不依赖 portfolio 交易约束，返回所有扫描到的标的（含 hold/reduce），
    让用户看到全貌后再决定。每标的 1 条（取 quality 排名记录，去重）。

    scope: 可选，按 scope 过滤最新任务（cn-stock/cn-etf/us-stock/us-etf），
    避免切换挖掘范围后丢失之前 scope 的结果。
    """
    return get_latest_scan_run_candidates(db, min_score=min_score, limit=limit, scope=scope)


@router.get("/discovery/tasks", response_model=list[DiscoveryTaskRead])
def list_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    scope: str | None = Query(default=None),
):
    """获取挖掘任务列表，支持按 scope 过滤。

    scope: 可选，cn-stock/cn-etf/us-stock/us-etf。
    传入 scope 时只返回该 scope 的任务，避免前端切换挖掘范围后显示错误 scope 的任务状态。
    """
    return list_discovery_tasks(limit=limit, scope=scope)


@router.get("/discovery/scopes/{scope}/stats", response_model=DiscoveryScopeStatsRead)
def get_scope_stats(scope: str, db: Session = Depends(get_db)):
    try:
        return get_discovery_scope_stats(scope, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/discovery/tasks/{task_id}", response_model=DiscoveryTaskRead)
def get_task(task_id: str):
    task = get_discovery_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Discovery task not found")
    return task


@router.post("/discovery/tasks/{task_id}/pause", response_model=DiscoveryTaskRead)
def pause_task(task_id: str):
    try:
        return pause_discovery_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/discovery/tasks/{task_id}/resume", response_model=DiscoveryTaskRead)
def resume_task(task_id: str):
    try:
        return resume_discovery_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/discovery/tasks/{task_id}/cancel", response_model=DiscoveryTaskRead)
def cancel_task(task_id: str):
    try:
        return cancel_discovery_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/discovery/tasks/{task_id}/retry", response_model=DiscoveryTaskRead)
def retry_task(task_id: str):
    try:
        return retry_discovery_task(task_id)
    except ValueError as exc:
        # 任务不存在返回 404；并发冲突返回 409
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/discovery/results/{scan_result_id}")
def patch_result(scan_result_id: int, payload: DiscoveryResultUpdate, db: Session = Depends(get_db)):
    result = update_discovery_result(db, scan_result_id, payload)
    if result is None:
        raise HTTPException(status_code=404, detail="Discovery result not found")
    return result


@router.post("/discovery/results/{scan_result_id}/refresh")
def refresh_result(scan_result_id: int, db: Session = Depends(get_db)):
    result = update_discovery_symbol(db, scan_result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Discovery result not found")
    return result


@router.post("/discovery/indicators/evaluate", response_model=list[DiscoveryIndicatorEvaluationRow])
def evaluate_indicators(payload: DiscoveryIndicatorEvaluateRequest, db: Session = Depends(get_db)):
    return evaluate_discovery_indicators(db, payload)


@router.post("/discovery/results/cleanup")
def cleanup_results(db: Session = Depends(get_db)):
    """Manually clean up expired non-frozen discovery results.
    Frozen results are never deleted by this endpoint."""
    result = cleanup_expired_discovery_results(db)
    return result


# ── P1：挖掘候选手动晋升 API ──
# 挖掘结果默认 is_promoted=0（不进候选池），用户手动点击"加入候选池"才晋升


@router.get("/discovery/candidates")
def list_discovery_candidates(
    scan_run_id: int | None = Query(default=None),
    is_promoted: int | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询候选列表，支持按 scan_run_id 和 is_promoted 过滤。"""
    return list_candidates(
        db, scan_run_id=scan_run_id, is_promoted=is_promoted, limit=limit, offset=offset
    )


@router.post("/discovery/candidates/{candidate_id}/promote")
def promote_discovery_candidate(candidate_id: int, db: Session = Depends(get_db)):
    """手动晋升单个候选（标记 is_promoted=1，加入候选池）。"""
    result = promote_candidate(db, candidate_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "candidate not found"))
    return result


@router.post("/discovery/candidates/{candidate_id}/unpromote")
def unpromote_discovery_candidate(candidate_id: int, db: Session = Depends(get_db)):
    """撤销晋升（从候选池移除，is_promoted=0）。"""
    result = unpromote_candidate(db, candidate_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "candidate not found"))
    return result


@router.post("/discovery/candidates/promote-batch")
def promote_discovery_candidates_batch(
    payload: dict, db: Session = Depends(get_db)
):
    """批量晋升候选。

    Body: {"candidate_ids": [1, 2, 3]}
    """
    candidate_ids = payload.get("candidate_ids") or []
    if not isinstance(candidate_ids, list) or not candidate_ids:
        raise HTTPException(status_code=400, detail="candidate_ids must be a non-empty list")
    # 校验元素类型
    if not all(isinstance(x, int) for x in candidate_ids):
        raise HTTPException(status_code=400, detail="candidate_ids must be integers")
    return promote_candidates_batch(db, candidate_ids)


# ── WP-P.4 数据准备与用户扫描分离 ──────────────────────────────
# 用户快速扫描链：同步执行，目标 < 5 分钟，绝不发起任何第三方 HTTP 请求
# 后台数据准备链：异步执行，不受 5 分钟约束


@router.post("/discovery/fast-scan", response_model=FastScanResponse)
def run_discovery_fast_scan(
    payload: FastScanRequest, db: Session = Depends(get_db)
) -> dict:
    """用户快速扫描（同步执行）。

    读取 ready 评分快照 → SQL 粗筛和排序 → Top-K 高级指标过滤 →
    组合约束过滤 → 保存小规模候选结果。

    严格约束：
    - 不发起任何第三方 HTTP 请求（market_data_sync / financial_report_task 等）
    - 只读 ready 状态快照，绝不读 building 半成品
    - 无可用快照时 10 秒内返回，degraded_reason="no_ready_snapshot"
    """
    return run_fast_scan(
        scope=payload.scope,
        min_score=payload.min_score,
        asset_types=payload.asset_types,
        stages=payload.stages,
        actions=payload.actions,
        indicator_plan=payload.indicator_plan,
        portfolio_id=payload.portfolio_id,
        portfolio_rule_id=payload.portfolio_rule_id,
        limit=payload.limit,
        db=db,
    )


@router.post("/discovery/data-prep", response_model=AsyncTaskRead)
def start_discovery_data_prep(payload: DataPrepRequest) -> dict:
    """启动后台数据准备任务（异步执行，不受 5 分钟 SLA 约束）。

    链路：行情增量同步 → 外部因子/宏观更新 → 因子与评分增量计算 →
          dirty 集合计算 → ready 评分快照生成 → 可选触发快速扫描。

    并发保护：同一 scope 同时只允许一个 data_prep 任务运行；
    若已有任务 queued/running，返回该任务而非创建新任务。
    """
    try:
        return start_data_prep_task(
            scope=payload.scope,
            trade_date=payload.trade_date,
            force_full_rebuild=payload.force_full_rebuild,
            trigger_fast_scan_after_ready=payload.trigger_fast_scan_after_ready,
            fast_scan_params=payload.fast_scan_params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/discovery/snapshot/status", response_model=SnapshotStatusRead)
def get_discovery_snapshot_status(
    scope: str = Query(..., description="挖掘范围（cn-stock/cn-etf/us-stock/us-etf）"),
    db: Session = Depends(get_db),
) -> dict:
    """查询某 scope 的快照状态与最近数据准备任务状态。

    用于机会中心展示：
    - 当前 ready 快照的生成时间 / 标的数 / dirty 数
    - 是否有 building 状态快照（数据准备进行中）
    - 最近一次 data_prep 任务的状态
    - 推荐下一步动作（无快照 → 启动数据准备；有快照 → 执行快速扫描）
    """
    normalized = _normalize_scope_for_snapshot(scope)

    # ready 快照
    ready = get_ready_snapshot(db, normalized)
    # building 快照
    building = is_snapshot_building_for_scope(db, normalized)

    # 最近一次 data_prep 任务（任意状态）
    last_data_prep_task_id: str | None = None
    last_data_prep_status: str | None = None
    try:
        stmt = (
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.task_type == TASK_TYPE_DATA_PREP)
            .order_by(desc(AsyncTaskRecord.created_at))
            .limit(20)
        )
        for task in db.execute(stmt).scalars().all():
            # 通过 payload_json 中的 scope 过滤（兼容连字符/下划线）
            try:
                payload = json.loads(task.payload_json or "{}") or {}
            except (TypeError, ValueError):
                payload = {}
            task_scope = payload.get("scope")
            if task_scope is None:
                continue
            if _normalize_scope_for_snapshot(str(task_scope)) == normalized:
                last_data_prep_task_id = task.id
                last_data_prep_status = task.status
                break
    except Exception:
        # 查询失败时只忽略 last_data_prep 字段，不影响主响应
        last_data_prep_task_id = None
        last_data_prep_status = None

    # WP-P.8：最近一次 fast_scan 任务的 timings / fast_scan_status
    # 数据来源：AsyncTaskRecord.result_json.timings 与 result_json.fast_scan_status
    # 当前 fast_scan 为同步执行，无 task 记录时为 None
    last_fast_scan_timings: dict | None = None
    last_fast_scan_status: str | None = None
    try:
        stmt = (
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.task_type == TASK_TYPE_FAST_SCAN)
            .order_by(desc(AsyncTaskRecord.created_at))
            .limit(20)
        )
        for task in db.execute(stmt).scalars().all():
            # 通过 payload_json 中的 scope 过滤（兼容连字符/下划线）
            try:
                payload = json.loads(task.payload_json or "{}") or {}
            except (TypeError, ValueError):
                payload = {}
            task_scope = payload.get("scope")
            if task_scope is None:
                continue
            if _normalize_scope_for_snapshot(str(task_scope)) == normalized:
                try:
                    result = json.loads(task.result_json or "{}") or {}
                except (TypeError, ValueError):
                    result = {}
                last_fast_scan_timings = result.get("timings")
                # fast_scan_status 优先取 result_json.fast_scan_status（ok/cancelled/timeout），
                # 兜底取 task.status（done/failed/cancelled）以保证字段非空
                last_fast_scan_status = result.get("fast_scan_status") or task.status
                break
    except Exception:
        # 查询失败时只忽略 last_fast_scan 字段，不影响主响应
        last_fast_scan_timings = None
        last_fast_scan_status = None

    # 推荐下一步动作
    recommended_action: str | None = None
    if ready is None and building is None:
        recommended_action = "无可用快照，请启动数据准备任务"
    elif ready is None and building is not None:
        recommended_action = "数据准备进行中，请稍后执行快速扫描"
    elif ready is not None and building is not None:
        recommended_action = "新版本快照构建中，当前可继续扫描旧快照"
    else:
        recommended_action = "快照已就绪，可执行快速扫描"

    return SnapshotStatusRead(
        scope=normalized,
        has_ready_snapshot=ready is not None,
        ready_snapshot_id=ready.id if ready else None,
        ready_snapshot_generated_at=ready.generated_at if ready else None,
        ready_snapshot_trade_date=ready.trade_date.date() if ready and ready.trade_date else None,
        ready_snapshot_symbol_count=ready.symbol_count if ready else None,
        ready_snapshot_dirty_symbol_count=ready.dirty_symbol_count if ready else None,
        has_building_snapshot=building is not None,
        building_snapshot_id=building.id if building else None,
        building_snapshot_created_at=building.created_at if building else None,
        last_data_prep_task_id=last_data_prep_task_id,
        last_data_prep_status=last_data_prep_status,
        recommended_action=recommended_action,
        last_fast_scan_timings=last_fast_scan_timings,
        last_fast_scan_status=last_fast_scan_status,
    ).model_dump()


# ── UAT-PAGES.2：已排除池 / 扫描记录 / 排除恢复 API ──────────────────────


@router.get("/discovery/excluded")
def list_excluded(
    symbol: str | None = Query(default=None, description="标的代码/名称子串筛选"),
    name: str | None = Query(default=None, description="标的名称子串筛选"),
    exclude_date_from: str | None = Query(
        default=None, description="排除时间下界（ISO 日期，UTC）"
    ),
    exclude_date_to: str | None = Query(
        default=None, description="排除时间上界（ISO 日期，UTC）"
    ),
    reason_type: str | None = Query(
        default=None, description="排除原因子串筛选（不区分大小写）"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询已排除候选列表。

    数据来源：opportunity_transition_events 中 event_type='exclude' 的记录，
    LEFT JOIN discovery_candidates 获取候选基础信息。

    支持筛选：标的代码/名称、排除时间区间、排除原因类型。
    """
    return list_excluded_candidates(
        db,
        symbol=symbol,
        name=name,
        exclude_date_from=exclude_date_from,
        exclude_date_to=exclude_date_to,
        reason_type=reason_type,
        limit=limit,
        offset=offset,
    )


@router.post("/discovery/candidates/{candidate_id}/exclude")
def exclude_candidate_route(
    candidate_id: int,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    """排除候选（写 opportunity_transition_events 审计事件，幂等）。

    Body（可选）：{"reason": "排除原因文本", "actor_type": "user"}
    """
    reason = None
    actor_type = "user"
    if payload:
        reason = payload.get("reason")
        actor_type = payload.get("actor_type") or "user"
    try:
        event = exclude_candidate_service(
            db,
            candidate_id=candidate_id,
            reason=reason,
            actor_type=actor_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if event is None:
        # 幂等：同幂等键已存在事件，返回已存在标记
        return {"ok": True, "candidate_id": candidate_id, "already_excluded": True}
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "event_id": event.id,
        "excluded_at": event.created_at.isoformat() if event.created_at else None,
        "already_excluded": False,
    }


@router.post("/discovery/candidates/{candidate_id}/restore")
def restore_candidate_route(
    candidate_id: int,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    """恢复已排除候选（写 opportunity_transition_events 审计事件，幂等）。

    支持两种恢复目标：
    - target="candidate"（默认）：仅写 EVENT_RESTORE 审计事件，候选回到候选池
    - target="observation"：除审计事件外，调用 transition_candidate_to_observation
                            将候选加入指定 watchlist_id 的观察池

    Body（可选）：
    {
        "target": "candidate" | "observation",
        "watchlist_id": 123,   // target=observation 时必填
        "note": "恢复原因",
        "priority": 0,
        "tags": ["tag1"],
        "target_portfolio_id": null,
        "actor_type": "user"
    }
    """
    target = "candidate"
    watchlist_id: int | None = None
    note: str | None = None
    priority = 0
    tags: list[str] | None = None
    target_portfolio_id: int | None = None
    actor_type = "user"
    if payload:
        target = payload.get("target") or "candidate"
        watchlist_id = payload.get("watchlist_id")
        note = payload.get("note")
        priority = int(payload.get("priority") or 0)
        tags = payload.get("tags")
        target_portfolio_id = payload.get("target_portfolio_id")
        actor_type = payload.get("actor_type") or "user"

    if target not in ("candidate", "observation"):
        raise HTTPException(
            status_code=400,
            detail="target 必须为 candidate 或 observation",
        )
    if target == "observation" and watchlist_id is None:
        raise HTTPException(
            status_code=400,
            detail="恢复到观察池时 watchlist_id 不能为空",
        )

    try:
        event = restore_candidate_service(
            db,
            candidate_id=candidate_id,
            actor_type=actor_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    observation_item_id: int | None = None
    if target == "observation" and watchlist_id is not None:
        try:
            item, _obs_event = transition_candidate_to_observation(
                db,
                candidate_id=candidate_id,
                watchlist_id=watchlist_id,
                note=note,
                priority=priority,
                tags=tags,
                target_portfolio_id=target_portfolio_id,
                actor_type=actor_type,
            )
            observation_item_id = item.id if item else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "candidate_id": candidate_id,
        "event_id": event.id if event else None,
        "restored_at": event.created_at.isoformat() if event else None,
        "target": target,
        "observation_item_id": observation_item_id,
        "already_restored": event is None,
    }


@router.get("/discovery/scan-runs")
def list_scan_runs_route(
    scope: str | None = Query(default=None, description="按 scope 过滤（cn-stock/cn-etf/us-stock/us-etf）"),
    trade_date: str | None = Query(default=None, description="按 trade_date 过滤（ISO 日期）"),
    status: str | None = Query(default=None, description="按扫描状态过滤（done/running/failed/cancelled）"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询扫描记录列表（ScanRun LEFT JOIN DiscoveryTaskRecord + DiscoveryScoreSnapshot）。

    展示字段：
    - 快照：snapshot_id/scope/trade_date/status/generated_at
    - 阶段耗时：stage_durations（来自 DiscoveryTaskRecord.stage_durations_json）
    - 参数：min_score/cache_key/portfolio_id/portfolio_rule_id
    - 缓存命中：cache_hit
    - 差异摘要：dirty_symbol_count/reused_score_count/rescored_count
    - 错误详情：snapshot_error_summary（来自 DiscoveryScoreSnapshot.error_summary_json）
    """
    return list_scan_runs(
        db,
        scope=scope,
        trade_date=trade_date,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/discovery/scan-runs/{scan_run_id}")
def get_scan_run_detail_route(
    scan_run_id: int,
    db: Session = Depends(get_db),
):
    """查询单条扫描记录详情（含 task_record 与 snapshot_record 完整信息）。"""
    detail = get_scan_run_detail(db, scan_run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Scan run not found")
    return detail