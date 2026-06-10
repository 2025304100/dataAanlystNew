from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import TradeSetupGenerateRequest, TradeSetupRead
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
