from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.score import ScoreCalculationRequest, ScoreRead
from app.services.analysis import calculate_symbol_score


router = APIRouter()


@router.post("/scores/calculate", response_model=list[ScoreRead])
def calculate_scores(payload: ScoreCalculationRequest, db: Session = Depends(get_db)):
    scores: list[Score] = []
    for symbol_id in payload.symbol_ids:
        symbol = db.get(Symbol, symbol_id)
        if symbol is None:
            raise HTTPException(status_code=404, detail=f"Symbol {symbol_id} not found")
        score = calculate_symbol_score(db, symbol, payload.trade_date)
        scores.append(score)
    db.commit()
    for score in scores:
        db.refresh(score)
    return scores


@router.get("/scores/latest/{symbol_id}", response_model=ScoreRead)
def get_latest_score(symbol_id: int, db: Session = Depends(get_db)):
    score = db.execute(
        select(Score).where(Score.symbol_id == symbol_id).order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()
    if score is None:
        raise HTTPException(status_code=404, detail="Score not found")
    return score


@router.get("/scores/history/{symbol_id}", response_model=list[ScoreRead])
def get_score_history(symbol_id: int, limit: int = 60, db: Session = Depends(get_db)):
    return db.execute(
        select(Score)
        .where(Score.symbol_id == symbol_id)
        .order_by(desc(Score.trade_date), desc(Score.id))
        .limit(limit)
    ).scalars().all()
