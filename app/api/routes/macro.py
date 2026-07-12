from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.async_utils import run_sync
from app.db.session import get_db
from app.schemas.async_task import AsyncTaskRead
from app.schemas.macro import MacroIndicatorRead, MacroOverviewResponse, MacroUpdateRequest
from app.services.macro import get_macro_indicator_history, get_macro_overview, update_macro_data
from app.services.macro_update_task import cancel_macro_update_task, create_macro_update_task, get_latest_macro_update_task, get_macro_update_task


router = APIRouter()


@router.get("/macro/overview", response_model=MacroOverviewResponse)
def macro_overview(
    region: str = Query(default="all", pattern="^(all|cn|us)$"),
    db: Session = Depends(get_db),
):
    return get_macro_overview(db, region=region)


@router.post("/macro/update", response_model=MacroOverviewResponse)
async def update_macro(payload: MacroUpdateRequest, db: Session = Depends(get_db)):
    return await run_sync(update_macro_data, db, payload.region)


@router.post("/macro/update-tasks", response_model=AsyncTaskRead)
def start_macro_update_task(payload: MacroUpdateRequest):
    return create_macro_update_task(payload)


@router.get("/macro/update-tasks/latest", response_model=AsyncTaskRead | None)
def latest_macro_update_task():
    return get_latest_macro_update_task()


@router.get("/macro/update-tasks/{task_id}", response_model=AsyncTaskRead)
def macro_update_task(task_id: str):
    task = get_macro_update_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Macro update task not found")
    return task


@router.post("/macro/update-tasks/{task_id}/cancel", response_model=AsyncTaskRead)
def cancel_macro_update(task_id: str):
    task = cancel_macro_update_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Macro update task not found")
    return task


@router.get("/macro/indicators/{indicator_key}/history", response_model=list[MacroIndicatorRead])
def macro_indicator_history(
    indicator_key: str,
    region: str = Query(pattern="^(cn|us)$"),
    limit: int = Query(default=60, ge=5, le=240),
    db: Session = Depends(get_db),
):
    return get_macro_indicator_history(db, region=region, indicator_key=indicator_key, limit=limit)
