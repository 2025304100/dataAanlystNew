from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.async_utils import run_sync
from app.db.session import get_db
from app.schemas.macro import MacroIndicatorRead, MacroOverviewResponse, MacroUpdateRequest
from app.services.macro import get_macro_indicator_history, get_macro_overview, update_macro_data


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


@router.get("/macro/indicators/{indicator_key}/history", response_model=list[MacroIndicatorRead])
def macro_indicator_history(
    indicator_key: str,
    region: str = Query(pattern="^(cn|us)$"),
    limit: int = Query(default=60, ge=5, le=240),
    db: Session = Depends(get_db),
):
    return get_macro_indicator_history(db, region=region, indicator_key=indicator_key, limit=limit)
