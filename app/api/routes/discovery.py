from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.discovery import DiscoveryResultUpdate, DiscoveryScopeStatsRead, DiscoveryTaskCreate, DiscoveryTaskRead
from app.services.discovery_tasks import (
    cancel_discovery_task,
    create_discovery_task,
    get_discovery_scope_stats,
    get_discovery_task,
    list_discovery_tasks,
    pause_discovery_task,
    resume_discovery_task,
)
from app.services.discovery_results import update_discovery_result, update_discovery_symbol


router = APIRouter()


@router.post("/discovery/tasks", response_model=DiscoveryTaskRead)
def create_task(payload: DiscoveryTaskCreate):
    return create_discovery_task(payload)


@router.get("/discovery/tasks", response_model=list[DiscoveryTaskRead])
def list_tasks(limit: int = Query(default=20, ge=1, le=100)):
    return list_discovery_tasks(limit=limit)


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
