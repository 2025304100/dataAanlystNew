from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.news import NewsUpdateRequest, NewsUpdateResponse
from app.services.news import get_latest_news, update_news


router = APIRouter()


@router.post("/news/update", response_model=NewsUpdateResponse)
def update_market_news(payload: NewsUpdateRequest, db: Session = Depends(get_db)):
    return update_news(db, payload)


@router.get("/news/latest", response_model=NewsUpdateResponse)
def get_latest_market_news(
    portfolio_id: int | None = None,
    symbol_ids: list[int] | None = Query(default=None),
    days: int = Query(default=7, ge=1, le=30),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return get_latest_news(db, portfolio_id=portfolio_id, symbol_ids=symbol_ids, days=days, limit=limit)
