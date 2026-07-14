from fastapi import APIRouter, HTTPException, Query

from app.schemas.async_task import AsyncTaskRead, FactorPipelineCreate
from app.services.async_tasks import (
    cancel_async_task,
    get_async_task,
    list_async_tasks,
)
from app.services.factors.pipeline_task import (
    TASK_TYPE,
    create_factor_pipeline_task,
)


router = APIRouter()


@router.post('/factor-pipeline/tasks', response_model=AsyncTaskRead)
def create_pipeline_task(payload: FactorPipelineCreate):
    try:
        return create_factor_pipeline_task(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get('/factor-pipeline/tasks', response_model=list[AsyncTaskRead])
def list_pipeline_tasks(limit: int = Query(default=20, ge=1, le=100)):
    return [
        task.model_dump()
        for task in list_async_tasks(task_type=TASK_TYPE, limit=limit)
    ]


@router.get(
    '/factor-pipeline/tasks/{task_id}',
    response_model=AsyncTaskRead,
)
def get_pipeline_task(task_id: str):
    task = get_async_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='Factor task not found')
    return task.model_dump()


@router.post(
    '/factor-pipeline/tasks/{task_id}/cancel',
    response_model=AsyncTaskRead,
)
def cancel_pipeline_task(task_id: str):
    try:
        return cancel_async_task(task_id).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
