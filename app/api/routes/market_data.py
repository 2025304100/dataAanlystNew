from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.schemas.market_data import DailyBarImportRequest, DailyBarRead, MarketDataUpdateRequest
from app.services.market_data import sync_market_data


router = APIRouter()


@router.post("/market-data/update")
def trigger_market_data_update(payload: MarketDataUpdateRequest, db: Session = Depends(get_db)) -> dict:
    try:
        result = sync_market_data(
            db=db,
            scope=payload.scope,
            watchlist_id=payload.watchlist_id,
            symbol_ids=payload.symbol_ids,
            asset_types=payload.asset_types,
            start_date=payload.start_date,
            end_date=payload.end_date,
            adjust=payload.adjust,
            auto_scan=payload.auto_scan,
            portfolio_id=payload.portfolio_id,
            portfolio_rule_id=payload.portfolio_rule_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "success": True,
        "message": "Market data sync completed",
        "data": result,
    }


@router.post("/market-data/bars/import")
def import_daily_bars(payload: DailyBarImportRequest, db: Session = Depends(get_db)):
    symbol = db.get(Symbol, payload.symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")

    imported = 0
    for item in payload.bars:
        existing = db.execute(
            select(DailyBar).where(DailyBar.symbol_id == payload.symbol_id, DailyBar.trade_date == item.trade_date)
        ).scalars().first()
        if existing is None:
            existing = DailyBar(symbol_id=payload.symbol_id, trade_date=item.trade_date)
            db.add(existing)
            imported += 1

        existing.open = item.open
        existing.high = item.high
        existing.low = item.low
        existing.close = item.close
        existing.volume = item.volume
        existing.amount = item.amount
        existing.turnover_rate = item.turnover_rate
        existing.source = item.source

    db.commit()
    return {"symbol_id": payload.symbol_id, "imported_count": imported, "total_rows": len(payload.bars)}


@router.get("/market-data/bars/{symbol_id}", response_model=list[DailyBarRead])
def get_daily_bars(symbol_id: int, limit: int = 120, db: Session = Depends(get_db)):
    return db.execute(
        select(DailyBar)
        .where(DailyBar.symbol_id == symbol_id)
        .order_by(DailyBar.trade_date.desc())
        .limit(limit)
    ).scalars().all()
