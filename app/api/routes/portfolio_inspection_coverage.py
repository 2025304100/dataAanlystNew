"""Preview and explicitly accept a verified current-account coverage start."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes.portfolio_governance import _ensure_portfolio_access, _load_portfolio, _require_actor
from app.db.session import get_db
from app.schemas.portfolio_reconciliation_coverage import CoverageAccepted, CoverageAcceptRequest, CoveragePreview, CoveragePreviewRequest
from app.services.portfolio_reconciliation_coverage import (
    CoverageError, accept_reconciliation_coverage, get_reconciliation_coverage,
    preview_reconciliation_coverage,
)
from app.services.data_governance_audit import write_audit_event
from datetime import datetime, timezone


router = APIRouter(prefix="/portfolios/{portfolio_id}/inspection/coverage", tags=["portfolio-inspection-coverage"])


def _access(db: Session, portfolio_id: int, actor: str):
    _load_portfolio(db, portfolio_id)
    _ensure_portfolio_access(db, portfolio_id, actor)


def _error(exc: CoverageError) -> HTTPException:
    return HTTPException(exc.status_code, detail={"error_code": exc.code, "message": str(exc), "blockers": exc.details})


def _failure_audit(db: Session, portfolio_id: int, actor: str, payload: CoverageAcceptRequest, code: str) -> bool:
    """An independent transaction after the rejected business transaction.

    If the audit store itself is unavailable, report that fact explicitly.
    Rolling back acceptance is never presented as evidence of a saved failure.
    """
    db.rollback()
    try:
        write_audit_event(db, "DATA_BLOCK_RESOLUTION", portfolio_id=portfolio_id,
            operator_id=actor, occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
            correlation_id=payload.acceptance_key, attributes={
                "original_action": "ACCOUNT_BASELINE_ACCEPTANCE_REJECTED", "actor_type": "human",
                "source_timezone": "UTC", "bypass": False,
                "initiator_id": actor, "executor_id": actor, "failure_code": code,
                "review_decision": "rejected", "execution_result": "not_executed",
                "outcome": "rejected", "coverage_start_trade_date": payload.coverage_start_trade_date.isoformat(),
                "evidence_hash": payload.expected_evidence_fingerprint, "review_note": payload.review_note,
                "history_gaps": [gap.model_dump(mode="json") for gap in payload.history_gaps],
            }, note="新覆盖起点未接受；此失败记录在业务回滚后独立保存")
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


@router.get("", summary="查看账户核验覆盖范围、代次和保留的历史未知区间")
def get_portfolio_inspection_coverage(portfolio_id: int, db: Session = Depends(get_db), actor: str = Depends(_require_actor)):
    _access(db, portfolio_id, actor)
    try:
        return get_reconciliation_coverage(db, portfolio_id)
    except SQLAlchemyError as exc:
        raise HTTPException(503, detail={"error_code": "COVERAGE_READ_FAILED", "message": "账户覆盖信息读取失败，请重试"}) from exc


@router.post("/preview", response_model=CoveragePreview, summary="只读核验现金、持仓、义务与新起点来源")
def preview_portfolio_inspection_coverage(portfolio_id: int, payload: CoveragePreviewRequest,
                                         db: Session = Depends(get_db), actor: str = Depends(_require_actor)):
    _access(db, portfolio_id, actor)
    try:
        return preview_reconciliation_coverage(db, portfolio_id, payload)
    except CoverageError as exc:
        raise _error(exc) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, detail={"error_code": "COVERAGE_EVIDENCE_READ_FAILED", "message": "当前账户来源读取失败，未形成可接受快照"}) from exc


@router.post("/accept", response_model=CoverageAccepted, summary="本人审核接受新覆盖起点，旧期保持未知")
def accept_portfolio_inspection_coverage(portfolio_id: int, payload: CoverageAcceptRequest,
                                        db: Session = Depends(get_db), actor: str = Depends(_require_actor)):
    _access(db, portfolio_id, actor)
    try:
        result = accept_reconciliation_coverage(db, portfolio_id, payload, operator_id=actor)
        db.commit()
        return result
    except CoverageError as exc:
        saved = _failure_audit(db, portfolio_id, actor, payload, exc.code)
        error = _error(exc)
        error.detail["failure_audit_saved"] = saved
        raise error from exc
    except IntegrityError as exc:
        saved = _failure_audit(db, portfolio_id, actor, payload, "COVERAGE_CONCURRENT_CHANGE")
        raise HTTPException(409, detail={"error_code": "COVERAGE_CONCURRENT_CHANGE", "message": "账户覆盖或审核记录已并发变化，请重新读取", "failure_audit_saved": saved}) from exc
    except SQLAlchemyError as exc:
        saved = _failure_audit(db, portfolio_id, actor, payload, "COVERAGE_ACCEPTANCE_NOT_SAVED")
        raise HTTPException(503, detail={"error_code": "COVERAGE_ACCEPTANCE_NOT_SAVED", "message": "新覆盖起点与成功审计未确认保存，请重试；账户未恢复", "failure_audit_saved": saved}) from exc
