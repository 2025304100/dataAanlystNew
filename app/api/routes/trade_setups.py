import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupGenerateRequest, TradeSetupRead, TradeSetupTrancheUpdateRequest
from app.services.trade_plans import get_latest_score, upsert_trade_setup


router = APIRouter()


@router.post("/trade-setups/generate", response_model=TradeSetupRead)
def generate_trade_setup(payload: TradeSetupGenerateRequest, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, payload.portfolio_id)
    symbol = db.get(Symbol, payload.symbol_id)
    score = db.get(Score, payload.score_id) if payload.score_id is not None else get_latest_score(db, payload.symbol_id)
    if portfolio is None or symbol is None or score is None:
        raise HTTPException(status_code=404, detail="Portfolio, symbol, or score not found")

    setup = upsert_trade_setup(
        db=db,
        portfolio_id=payload.portfolio_id,
        symbol=symbol,
        score=score,
        scan_run_id=payload.scan_run_id,
        overrides=payload.overrides.model_dump(exclude_unset=True) if payload.overrides is not None else None
    )
    db.commit()
    db.refresh(setup)
    return setup


@router.get("/trade-setups/latest/{symbol_id}", response_model=TradeSetupRead)
def get_latest_trade_setup(
    symbol_id: int,
    portfolio_id: int = Query(...),
    db: Session = Depends(get_db),
):
    setup = db.execute(
        select(TradeSetup)
        .where(TradeSetup.symbol_id == symbol_id, TradeSetup.portfolio_id == portfolio_id)
        .order_by(desc(TradeSetup.id))
    ).scalars().first()
    if setup is None:
        raise HTTPException(status_code=404, detail="Trade setup not found")
    return setup





@router.patch("/trade-setups/{setup_id}/tranches", response_model=TradeSetupRead)
def update_trade_setup_tranches(setup_id: int, payload: TradeSetupTrancheUpdateRequest, db: Session = Depends(get_db)):
    setup = db.get(TradeSetup, setup_id)
    if setup is None:
        raise HTTPException(status_code=404, detail="Trade setup not found")

    tranches = [item.model_dump() for item in payload.tranche_plan]
    total_pct = sum(float(item.get("position_pct") or 0) for item in tranches)
    if total_pct > max(float(setup.recommended_position_pct or 0), 0) + 0.0001:
        raise HTTPException(status_code=400, detail="Tranche total cannot exceed recommended position")

    setup.manual_tranche_plan_json = json.dumps(tranches, ensure_ascii=False) if tranches else None
    db.commit()
    db.refresh(setup)
    return setup
