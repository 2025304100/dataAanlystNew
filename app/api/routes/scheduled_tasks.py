from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.scheduled_task import ScheduledTask, ScheduledTaskRun
from app.schemas.scheduled_task import (
    ScheduledTaskCreate,
    ScheduledTaskDefinitionRead,
    ScheduledTaskRead,
    ScheduledTaskRunRead,
    ScheduledTaskUpdate,
)
from app.services.scheduled_tasks import (
    TASK_DEFINITIONS,
    create_schedule,
    delete_schedule,
    execute_schedule,
    run_to_dict,
    schedule_to_dict,
    update_schedule,
)


router = APIRouter()


@router.get(
    "/scheduled-tasks/definitions",
    response_model=list[ScheduledTaskDefinitionRead],
)
def list_scheduled_task_definitions():
    return [
        {"task_type": key, **value}
        for key, value in TASK_DEFINITIONS.items()
    ]


@router.get(
    "/scheduled-tasks",
    response_model=list[ScheduledTaskRead],
)
def list_scheduled_tasks(db: Session = Depends(get_db)):
    rows = db.execute(
        select(ScheduledTask).order_by(ScheduledTask.id)
    ).scalars().all()
    return [schedule_to_dict(db, item) for item in rows]


@router.get(
    "/scheduled-tasks/runs",
    response_model=list[ScheduledTaskRunRead],
)
def list_all_scheduled_task_runs(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(ScheduledTaskRun)
        .order_by(desc(ScheduledTaskRun.created_at), desc(ScheduledTaskRun.id))
        .limit(limit)
    ).scalars().all()
    return [run_to_dict(db, item) for item in rows]


@router.post(
    "/scheduled-tasks",
    response_model=ScheduledTaskRead,
)
def create_scheduled_task(
    payload: ScheduledTaskCreate,
    db: Session = Depends(get_db),
):
    try:
        return schedule_to_dict(db, create_schedule(db, payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/scheduled-tasks/{schedule_id}",
    response_model=ScheduledTaskRead,
)
def get_scheduled_task(
    schedule_id: int,
    db: Session = Depends(get_db),
):
    item = db.get(ScheduledTask, schedule_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return schedule_to_dict(db, item)


@router.patch(
    "/scheduled-tasks/{schedule_id}",
    response_model=ScheduledTaskRead,
)
def patch_scheduled_task(
    schedule_id: int,
    payload: ScheduledTaskUpdate,
    db: Session = Depends(get_db),
):
    try:
        return schedule_to_dict(
            db,
            update_schedule(db, schedule_id, payload),
        )
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/scheduled-tasks/{schedule_id}")
def remove_scheduled_task(
    schedule_id: int,
    db: Session = Depends(get_db),
):
    try:
        delete_schedule(db, schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "id": schedule_id}


@router.post(
    "/scheduled-tasks/{schedule_id}/run",
    response_model=ScheduledTaskRunRead,
)
def run_scheduled_task_now(
    schedule_id: int,
    db: Session = Depends(get_db),
):
    try:
        run = execute_schedule(db, schedule_id, trigger_source="manual")
        return run_to_dict(db, run)
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/scheduled-tasks/{schedule_id}/runs",
    response_model=list[ScheduledTaskRunRead],
)
def list_scheduled_task_runs(
    schedule_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    if db.get(ScheduledTask, schedule_id) is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    rows = db.execute(
        select(ScheduledTaskRun)
        .where(ScheduledTaskRun.schedule_id == schedule_id)
        .order_by(desc(ScheduledTaskRun.created_at), desc(ScheduledTaskRun.id))
        .limit(limit)
    ).scalars().all()
    return [run_to_dict(db, item) for item in rows]
