"""HF-07 read-only inventory and separately reviewed new-admission controls."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes.portfolio_governance import _ensure_portfolio_access, _header_role, _load_portfolio, _require_actor
from app.db.session import get_db
from app.models.g5_dual_run import G5DualRunReport
from app.schemas.g5_admission_governance import G5AdmissionControlRequest, G5ReportInvalidationRequest
from app.services.g5_admission_governance import (
    G5GovernanceError, append_g5_report_invalidation, get_g5_admission_state, set_g5_admission_freeze,
)
from app.services.g5_legacy_inventory import build_g5_legacy_inventory

router = APIRouter(prefix="/g5-admission", tags=["g5-admission-governance"])


def _access(db, portfolio_id, actor, role, *, write=False):
    # The existing portfolio-access helper is deliberately permissive until a
    # real ownership ACL exists. Do not use it as authority for HF07 writes.
    if write and role != "admin":
        raise HTTPException(403, detail={"error_code": "G5_GOVERNANCE_REVIEWER_REQUIRED",
            "message": "新准入冻结、报告隔离及恢复均需管理员审核"})
    if portfolio_id is None:
        if role not in ({"admin"} if write else {"admin", "auditor"}):
            raise HTTPException(403, detail={"error_code": "G5_GOVERNANCE_REVIEWER_REQUIRED",
                "message": "全局盘点需审计权限；全局冻结或恢复需管理员审核"})
    else:
        _load_portfolio(db, portfolio_id)
        _ensure_portfolio_access(db, portfolio_id, actor)


def _error(exc):
    return HTTPException(exc.status_code, detail={"error_code": exc.code, "message": str(exc), "blockers": exc.details})


@router.get("/inventory", summary="HF07：只读盘点原报告与真实采用引用，不自动隔离")
def inventory(portfolio_id: int | None = Query(default=None, gt=0), db: Session = Depends(get_db),
        actor: str = Depends(_require_actor), role: str | None = Depends(_header_role)):
    _access(db, portfolio_id, actor, role)
    try:
        return build_g5_legacy_inventory(db, portfolio_id=portfolio_id)
    except SQLAlchemyError as exc:
        raise HTTPException(503, detail={"error_code": "G5_INVENTORY_UNAVAILABLE", "message": "盘点来源读取失败，未确认任何处理结论"}) from exc


@router.get("/control", summary="读取新准入冻结状态；不影响已有日常交易门禁")
def control(portfolio_id: int | None = Query(default=None, gt=0), db: Session = Depends(get_db),
        actor: str = Depends(_require_actor), role: str | None = Depends(_header_role)):
    _access(db, portfolio_id, actor, role)
    return get_g5_admission_state(db, portfolio_id)


@router.post("/control", summary="审核冻结或恢复新的G6/G7/来源切换准入")
def set_control(payload: G5AdmissionControlRequest, db: Session = Depends(get_db),
        actor: str = Depends(_require_actor), role: str | None = Depends(_header_role)):
    _access(db, payload.portfolio_id, actor, role, write=True)
    try:
        result = set_g5_admission_freeze(db, **payload.model_dump(), operator_id=actor)
        db.commit()
        return result
    except G5GovernanceError as exc:
        db.rollback()
        raise _error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, detail={"error_code": "G5_GOVERNANCE_CONCURRENT_CHANGE", "message": "处置记录并发变化，请重新读取后审核"}) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(503, detail={"error_code": "G5_GOVERNANCE_NOT_SAVED", "message": "冻结或恢复记录未确认保存，请重新读取核验"}) from exc


@router.post("/reports/{report_id}/invalidation", summary="按原哈希追加隔离或恢复；保留原报告与审计")
def invalidate_report(report_id: int, payload: G5ReportInvalidationRequest, db: Session = Depends(get_db),
        actor: str = Depends(_require_actor), role: str | None = Depends(_header_role)):
    report = db.get(G5DualRunReport, report_id)
    if report is None:
        raise HTTPException(404, detail={"error_code": "G5_REPORT_NOT_FOUND", "message": "原报告不存在"})
    _access(db, report.portfolio_id, actor, role, write=True)
    try:
        result = append_g5_report_invalidation(db, report_id, **payload.model_dump(), operator_id=actor)
        db.commit()
        return result
    except G5GovernanceError as exc:
        db.rollback()
        raise _error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, detail={"error_code": "G5_GOVERNANCE_CONCURRENT_CHANGE", "message": "隔离记录并发变化，请重新核实原件"}) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(503, detail={"error_code": "G5_INVALIDATION_NOT_SAVED", "message": "追加处置未确认保存，请重新读取；原报告保留"}) from exc
