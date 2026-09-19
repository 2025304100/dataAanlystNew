from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.scan import ScanResult, ScanRun
from app.schemas.scan import ScanResultRead, ScanRunCreate
from app.services.scans import run_scan


router = APIRouter()


@router.post("/scans/runs")
def create_scan_run(payload: ScanRunCreate, db: Session = Depends(get_db)):
    scan_run = run_scan(
        db=db,
        scope_snapshot=payload.scope_snapshot,
        filters_snapshot=payload.filters_snapshot,
        portfolio_id=payload.portfolio_id,
        portfolio_rule_id=payload.portfolio_rule_id,
        run_name=payload.run_name,
        preset_id=payload.preset_id,
    )
    db.commit()
    db.refresh(scan_run)
    return {"scan_run_id": scan_run.id, "status": scan_run.status}


@router.get("/scans/runs/{scan_run_id}")
def get_scan_run(scan_run_id: int, db: Session = Depends(get_db)):
    scan_run = db.get(ScanRun, scan_run_id)
    if scan_run is None:
        raise HTTPException(status_code=404, detail="Scan run not found")
    return {
        "id": scan_run.id,
        "run_name": scan_run.run_name,
        "status": scan_run.status,
        "started_at": scan_run.started_at,
        "finished_at": scan_run.finished_at,
    }


@router.get("/scans/runs/{scan_run_id}/results", response_model=list[ScanResultRead])
def get_scan_results(
    scan_run_id: int,
    result_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    stmt = select(ScanResult).where(ScanResult.scan_run_id == scan_run_id)
    if result_type:
        stmt = stmt.where(ScanResult.result_type == result_type)
    stmt = stmt.order_by(ScanResult.is_frozen.desc(), ScanResult.priority_score.desc(), ScanResult.created_at.desc())
    return db.execute(stmt).scalars().all()


@router.get("/scans/latest/executable", response_model=list[ScanResultRead])
def get_latest_executable_candidates(
    portfolio_id: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(ScanRun).where(ScanRun.status == "done")
    if portfolio_id is not None:
        stmt = stmt.where(ScanRun.portfolio_id == portfolio_id)
    latest_run = db.execute(stmt.order_by(desc(ScanRun.id))).scalars().first()
    if latest_run is None:
        return []
    results = db.execute(
        select(ScanResult)
        .where(ScanResult.scan_run_id == latest_run.id, ScanResult.result_type == "executable")
        .order_by(ScanResult.is_frozen.desc(), ScanResult.priority_score.desc(), ScanResult.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return results
