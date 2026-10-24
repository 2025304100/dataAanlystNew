"""Portfolio inspection and confirmed data-only repair endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.routes import portfolio_governance as governance
from app.schemas.portfolio_inspection import (
    InspectionRepairPlan, InspectionRepairRetry, InspectionRepairSubmit,
    InspectionRepairTaskResponse, InspectionRepairTaskListResponse, PortfolioInspectionResponse,
)
from app.services.portfolio_inspection_diagnostics import build_portfolio_inspection
from app.services.portfolio_inspection_repair import (
    InspectionRepairError, cancel_inspection_repair, get_inspection_repair_task,
    list_inspection_repair_tasks, retry_inspection_repair, submit_inspection_repair,
)

router = APIRouter(prefix="/portfolios/{portfolio_id}/inspection", tags=["portfolio-inspection"])


def _authorize(db: Session, portfolio_id: int, actor: str) -> None:
    with db.no_autoflush:
        governance._load_portfolio(db, portfolio_id)
        governance._ensure_portfolio_access(db, portfolio_id, actor)


def _execute(callback):
    try:
        return callback()
    except LookupError as exc:
        raise HTTPException(404, detail={"error_code": "INSPECTION_RESOURCE_NOT_FOUND", "message": str(exc)}) from None
    except InspectionRepairError as exc:
        raise HTTPException(exc.status_code, detail={"error_code": exc.code, "message": str(exc),
            "retryable": exc.status_code == 503}) from None


@router.get("/data-diagnostics", response_model=PortfolioInspectionResponse)
def data_diagnostics(portfolio_id: int, db: Session = Depends(get_db), actor: str = Depends(governance._require_actor)):
    _authorize(db, portfolio_id, actor)
    return _execute(lambda: build_portfolio_inspection(db, portfolio_id))


@router.get("/repair-plan", response_model=InspectionRepairPlan)
def repair_plan(portfolio_id: int, db: Session = Depends(get_db), actor: str = Depends(governance._require_actor)):
    _authorize(db, portfolio_id, actor)
    return _execute(lambda: build_portfolio_inspection(db, portfolio_id)["repair_plan"])


@router.post("/repairs", response_model=InspectionRepairTaskResponse, status_code=202)
def submit_repair(portfolio_id: int, payload: InspectionRepairSubmit, db: Session = Depends(get_db), actor: str = Depends(governance._require_actor)):
    _authorize(db, portfolio_id, actor)
    return _execute(lambda: submit_inspection_repair(db, portfolio_id, payload, actor=actor))


@router.get("/repairs/{task_id}", response_model=InspectionRepairTaskResponse)
def repair_task(portfolio_id: int, task_id: str, db: Session = Depends(get_db), actor: str = Depends(governance._require_actor)):
    _authorize(db, portfolio_id, actor)
    return _execute(lambda: get_inspection_repair_task(db, portfolio_id, task_id))


@router.get("/repairs", response_model=InspectionRepairTaskListResponse)
def repair_tasks(portfolio_id: int, limit: int = Query(20, ge=1, le=100), cursor: str | None = None,
                 db: Session = Depends(get_db), actor: str = Depends(governance._require_actor)):
    _authorize(db, portfolio_id, actor)
    return _execute(lambda: list_inspection_repair_tasks(db, portfolio_id, limit=limit, cursor=cursor))


@router.post("/repairs/{task_id}/cancel", response_model=InspectionRepairTaskResponse)
def cancel_repair(portfolio_id: int, task_id: str, db: Session = Depends(get_db), actor: str = Depends(governance._require_actor)):
    _authorize(db, portfolio_id, actor)
    return _execute(lambda: cancel_inspection_repair(db, portfolio_id, task_id, actor=actor))


@router.post("/repairs/{task_id}/retry", response_model=InspectionRepairTaskResponse, status_code=202)
def retry_repair(portfolio_id: int, task_id: str, payload: InspectionRepairRetry, db: Session = Depends(get_db), actor: str = Depends(governance._require_actor)):
    _authorize(db, portfolio_id, actor)
    return _execute(lambda: retry_inspection_repair(db, portfolio_id, task_id, idempotency_key=payload.idempotency_key, actor=actor))
