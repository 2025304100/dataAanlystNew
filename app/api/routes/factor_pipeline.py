from fastapi import APIRouter, HTTPException, Query

from app.schemas.async_task import AsyncTaskRead, FactorPipelineCreate
from app.services.async_tasks import (
    cancel_async_task,
    get_async_task,
    list_async_tasks,
)
from app.services.factors.pipeline_task import (
    FactorPipelineBindingError,
    TASK_TYPE,
    create_factor_pipeline_task,
    get_pipeline_eta,
)


router = APIRouter()


@router.get('/factor-pipeline/eta')
def get_eta(
    train_model: bool = Query(default=True),
    full_refresh: bool = Query(default=False),
):
    """返回基于历史已完成任务的预估总耗时（秒）。"""
    return get_pipeline_eta(
        train_model=train_model,
        full_refresh=full_refresh,
    )


@router.post('/factor-pipeline/tasks', response_model=AsyncTaskRead)
def create_pipeline_task(payload: FactorPipelineCreate):
    try:
        return create_factor_pipeline_task(payload)
    except FactorPipelineBindingError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
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
