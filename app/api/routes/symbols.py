from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.symbol import SymbolCreate, SymbolRead
from app.services.regions import region_from_market
from app.services.symbol_names import refresh_symbol_name


router = APIRouter()


def _serialize_symbol(symbol: Symbol) -> SymbolRead:
    return SymbolRead.model_validate({**symbol.__dict__, "region": region_from_market(symbol.market)})


@router.get("/symbols", response_model=list[SymbolRead])
def list_symbols(
    keyword: str | None = Query(default=None),
    asset_type: str | None = Query(default=None),
    market: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = select(Symbol)
    if keyword:
        like_pattern = f"%{keyword}%"
        stmt = stmt.where((Symbol.symbol.like(like_pattern)) | (Symbol.name.like(like_pattern)))
    if asset_type:
        stmt = stmt.where(Symbol.asset_type == asset_type)
    if market:
        stmt = stmt.where(Symbol.market == market)
    stmt = stmt.order_by(Symbol.id.desc()).offset((page - 1) * page_size).limit(page_size)
    return [_serialize_symbol(symbol) for symbol in db.execute(stmt).scalars().all()]


@router.post("/symbols", response_model=SymbolRead)
def create_symbol(payload: SymbolCreate, db: Session = Depends(get_db)):
    existing = db.execute(select(Symbol).where(Symbol.symbol == payload.symbol)).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Symbol already exists")
    symbol = Symbol(**payload.model_dump())
    refresh_symbol_name(symbol)
    db.add(symbol)
    db.commit()
    db.refresh(symbol)
    return _serialize_symbol(symbol)


@router.get("/symbols/{symbol_id}")
def get_symbol_detail(symbol_id: int, db: Session = Depends(get_db)):
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")
    latest_score = db.execute(
        select(Score).where(Score.symbol_id == symbol_id).order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()
    return {
        "symbol": _serialize_symbol(symbol),
        "latest_score": None
        if latest_score is None
        else {
            "quality_score": latest_score.quality_score,
            "quality_grade": latest_score.quality_grade,
            "timing_score": latest_score.timing_score,
            "stage": latest_score.stage,
            "action": latest_score.action,
            "priority_score": latest_score.priority_score,
            "trade_date": latest_score.trade_date,
            # 股质评分分项
            "trend_score": latest_score.trend_score,
            "momentum_score": latest_score.momentum_score,
            "volatility_score": latest_score.volatility_score,
            "liquidity_score": latest_score.liquidity_score,
            "breadth_score": latest_score.breadth_score,
            "event_score": latest_score.event_score,
            # 时点评分分项（P1 新增）
            "breakout_score": latest_score.breakout_score,
            "pullback_score": latest_score.pullback_score,
            "overheat_penalty": latest_score.overheat_penalty,
            # 数据可信度（P0-4.3）
            "data_credibility": latest_score.data_credibility,
        },
    }
