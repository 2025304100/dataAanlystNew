"""Personal ETF history and reviewed research inputs."""
from datetime import date
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.services import etf_history_preparation as service
from app.services import etf_training_list

router = APIRouter(prefix='/etf-history')


class TrainingListRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    codes_text: str = Field(max_length=2000)
    expected_list_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.get('/training-list')
def training_list(db: Session = Depends(get_db)):
    try:
        return etf_training_list.read_list(db)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put('/training-list')
def save_training_list(payload: TrainingListRequest, db: Session = Depends(get_db)):
    try:
        return etf_training_list.save_list(db, payload.codes_text, payload.expected_list_sha256)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get('/training-list/scope')
def training_list_scope(start_date: date | None = None, end_date: date | None = None,
                        db: Session = Depends(get_db)):
    try:
        return etf_training_list.preparation_scope(db, start_date, end_date)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


class TrainingPreparationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    start_date: date
    end_date: date
    expected_scope_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.post('/training-list/preparations', status_code=202)
def start_training_preparation(payload: TrainingPreparationRequest, db: Session = Depends(get_db)):
    try:
        return etf_training_list.create_preparation(db, payload)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get('/training-list/preparations/requests/{request_id}')
def recover_training_preparation(request_id: UUID, db: Session = Depends(get_db)):
    try:
        return etf_training_list.find_request(db, request_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get('/training-list/preparations/{task_id}')
def training_preparation(task_id: str, db: Session = Depends(get_db)):
    try:
        value = etf_training_list.read_preparation(db, task_id)
        if value is None:
            raise HTTPException(status_code=404, detail='没有找到这次ETF训练历史数据准备记录，请重新读取。')
        return value
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


class PreparationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    portfolio_id: int = Field(ge=1)
    request_id: UUID
    start_date: date
    end_date: date
    expected_scope_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.post('/preparations', status_code=202)
def start(payload: PreparationRequest, db: Session = Depends(get_db)):
    try:
        return service.create_preparation(db, payload)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get('/preparations/requests/{request_id}')
def recover(request_id: UUID, portfolio_id: int = Query(ge=1), db: Session = Depends(get_db)):
    try:
        return service.find_request(db, portfolio_id, request_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get('/preparations/{task_id}')
def result(task_id: str, db: Session = Depends(get_db)):
    try:
        value = service.read_preparation(db, task_id)
        if value is None:
            raise HTTPException(status_code=404, detail='没有找到这次ETF历史数据补充记录，请重新读取。')
        return value
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get('/datasets')
def list_datasets():
    return service.list_datasets()


@router.get('/training-options')
def training_options(factor_set_id: str = Query(min_length=1, max_length=64), use_training_list: bool = False,
                     dataset_id: str | None = Query(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'),
                     manifest_sha256: str | None = Query(default=None, pattern=r'^[a-f0-9]{64}$'),
                     expected_list_sha256: str | None = Query(default=None, pattern=r'^[a-f0-9]{64}$'),
                     db: Session = Depends(get_db)):
    try:
        return service.training_options(db, factor_set_id, use_training_list,
                                        dataset_id, manifest_sha256, expected_list_sha256)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get('/scope')
def scope(portfolio_id: int = Query(ge=1), start_date: date | None = None,
          end_date: date | None = None, db: Session = Depends(get_db)):
    try:
        return service.preparation_scope(db, portfolio_id, start_date, end_date)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
