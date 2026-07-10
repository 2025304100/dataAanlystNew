from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.discovery import (
    DiscoveryIndicatorEvaluateRequest,
    DiscoveryIndicatorEvaluationRow,
    DiscoveryResultUpdate,
    DiscoveryScopeStatsRead,
    DiscoveryTaskCreate,
    DiscoveryTaskRead,
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