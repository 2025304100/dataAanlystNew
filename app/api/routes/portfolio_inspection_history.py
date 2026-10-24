"""Read-only inspection history, isolated to one portfolio."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes.portfolio_governance import _ensure_portfolio_access, _load_portfolio, _require_actor
from app.db.session import get_db
from app.schemas.portfolio_inspection_history import (
    ExecutionResult, HistoryBatchDetail, HistoryFilters, HistoryPage,
    HistorySecurityPage, HistoryMaterial, ReviewDecision,
)
from app.services.portfolio_inspection_evidence import get_inspection_history_material, export_inspection_history_evidence
from app.services.portfolio_inspection_history import (
    get_inspection_history_batch, normalized_filters, query_inspection_history,
    search_inspection_history_securities,
)


router = APIRouter(prefix="/portfolios/{portfolio_id}/inspection/history", tags=["portfolio-inspection-history"])


def _filters(
    occurred_from: datetime | None = Query(None),
    occurred_to: datetime | None = Query(None),
    trade_date_from: date | None = Query(None),
    trade_date_to: date | None = Query(None),
    security_id: list[str] | None = Query(None, description="带市场身份的证券ID；重复参数任一命中"),
    action: list[str] | None = Query(None),
    review_decision: ReviewDecision | None = Query(None),
    execution_result: ExecutionResult | None = Query(None),
    operator: str | None = Query(None, max_length=128),
    actor_type: str | None = Query(None, max_length=32),
    failure_code: str | None = Query(None, max_length=128),
    ai_assisted: bool | None = Query(None),
    since_last_reconciliation: bool = Query(False),
    q: str | None = Query(None, max_length=200),
    issue_id: str | None = Query(None, max_length=128),
    correlation_id: str | None = Query(None, max_length=128),
    report_ref: str | None = Query(None, max_length=256),
    report_version: str | None = Query(None, max_length=128),
) -> HistoryFilters:
    try:
        return normalized_filters(HistoryFilters(
            occurred_from=occurred_from, occurred_to=occurred_to,
            trade_date_from=trade_date_from, trade_date_to=trade_date_to,
            security_ids=security_id or [], actions=action or [],
            review_decision=review_decision, execution_result=execution_result,
            operator=operator, actor_type=actor_type, failure_code=failure_code, ai_assisted=ai_assisted, q=q, issue_id=issue_id,
            since_last_reconciliation=since_last_reconciliation,
            correlation_id=correlation_id, report_ref=report_ref, report_version=report_version,
        ))
    except ValueError as exc:
        raise HTTPException(400, detail={"error_code": "INVALID_FILTER", "message": str(exc)}) from exc


def _authorize(db: Session, portfolio_id: int, actor: str) -> None:
    _load_portfolio(db, portfolio_id)
    _ensure_portfolio_access(db, portfolio_id, actor)


def _read_failure(exc: Exception) -> HTTPException:
    # A read failure must never be returned as an empty or successful history.
    return HTTPException(503, detail={
        "error_code": "HISTORY_READ_FAILED", "message": "历史记录读取失败，请保留当前筛选并重试",
    })


@router.get("", response_model=HistoryPage, summary="按真实审计批次查询当前组合历史")
def get_portfolio_inspection_history(
    portfolio_id: int,
    filters: HistoryFilters = Depends(_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    snapshot_event_id: int | None = Query(None, ge=0),
    db: Session = Depends(get_db),
    actor: str = Depends(_require_actor),
) -> HistoryPage:
    try:
        _authorize(db, portfolio_id, actor)
        return query_inspection_history(db, portfolio_id, filters, page=page,
                                        page_size=page_size, snapshot_event_id=snapshot_event_id)
    except ValueError as exc:
        raise HTTPException(400, detail={"error_code": "INVALID_FILTER", "message": str(exc)}) from exc
    except SQLAlchemyError as exc:
        raise _read_failure(exc) from exc


@router.get("/securities", response_model=HistorySecurityPage, summary="按名称或代码搜索当前组合历史索引标的")
def get_portfolio_inspection_history_securities(
    portfolio_id: int,
    q: str | None = Query(None, max_length=128),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    actor: str = Depends(_require_actor),
) -> HistorySecurityPage:
    try:
        _authorize(db, portfolio_id, actor)
        return search_inspection_history_securities(db, portfolio_id, q=q, limit=limit)
    except SQLAlchemyError as exc:
        raise _read_failure(exc) from exc


@router.get("/material", response_model=HistoryMaterial, summary="只读定位原报告版本与关联来源，明确材料缺口")
def get_portfolio_inspection_history_material(
    portfolio_id: int, source_event_id: int = Query(..., ge=1),
    kind: str = Query(..., min_length=1, max_length=32), ref: str = Query(..., min_length=1, max_length=256),
    version: str | None = Query(None, max_length=128), snapshot_event_id: int | None = Query(None, ge=0),
    db: Session = Depends(get_db), actor: str = Depends(_require_actor),
) -> HistoryMaterial:
    try:
        _authorize(db, portfolio_id, actor)
        material = get_inspection_history_material(db, portfolio_id, source_event_id=source_event_id,
            kind=kind, ref=ref, version=version, snapshot_event_id=snapshot_event_id)
        if material is None:
            raise HTTPException(404, detail={"error_code": "HISTORY_REFERENCE_NOT_FOUND", "message": "当前组合及查询水位中未找到此真实关联引用"})
        return material
    except SQLAlchemyError as exc:
        raise _read_failure(exc) from exc


@router.get("/export", summary="导出当前筛选下完整处置批次、原快照、缺失证据及时间线")
def export_portfolio_inspection_history(
    portfolio_id: int, filters: HistoryFilters = Depends(_filters),
    snapshot_event_id: int | None = Query(None, ge=0),
    db: Session = Depends(get_db), actor: str = Depends(_require_actor),
):
    try:
        _authorize(db, portfolio_id, actor)
        package = export_inspection_history_evidence(db, portfolio_id, filters, snapshot_event_id=snapshot_event_id, actor=actor)
        return JSONResponse(jsonable_encoder(package), headers={
            "Content-Disposition": f'attachment; filename="portfolio-{portfolio_id}-inspection-evidence.json"',
            "Cache-Control": "no-store",
        })
    except ValueError as exc:
        raise HTTPException(400, detail={"error_code": "INVALID_FILTER", "message": str(exc)}) from exc
    except SQLAlchemyError as exc:
        raise _read_failure(exc) from exc


@router.get("/{batch_id}", response_model=HistoryBatchDetail, summary="只读回看整批对象、原报告引用与处理经过")
def get_portfolio_inspection_history_detail(
    portfolio_id: int,
    batch_id: str,
    filters: HistoryFilters = Depends(_filters),
    snapshot_event_id: int | None = Query(None, ge=0),
    db: Session = Depends(get_db),
    actor: str = Depends(_require_actor),
) -> HistoryBatchDetail:
    try:
        _authorize(db, portfolio_id, actor)
        batch = get_inspection_history_batch(db, portfolio_id, batch_id, filters=filters,
                                             snapshot_event_id=snapshot_event_id)
        if batch is None:
            raise HTTPException(404, detail={"error_code": "HISTORY_BATCH_NOT_FOUND", "message": "当前组合未找到该历史批次"})
        return batch
    except SQLAlchemyError as exc:
        raise _read_failure(exc) from exc
