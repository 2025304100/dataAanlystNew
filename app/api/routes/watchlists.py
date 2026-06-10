from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.watchlist import WatchlistCreate, WatchlistItemCreate, WatchlistItemRead, WatchlistRead


router = APIRouter()


@router.get("/watchlists", response_model=list[WatchlistRead])
def list_watchlists(db: Session = Depends(get_db)):
    return db.execute(select(Watchlist).order_by(Watchlist.id.desc())).scalars().all()


@router.post("/watchlists", response_model=WatchlistRead)
def create_watchlist(payload: WatchlistCreate, db: Session = Depends(get_db)):
    existing = db.execute(select(Watchlist).where(Watchlist.name == payload.name)).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Watchlist already exists")
    watchlist = Watchlist(**payload.model_dump())
    db.add(watchlist)
    db.commit()
    db.refresh(watchlist)
    return watchlist


@router.get("/watchlists/{watchlist_id}/items", response_model=list[WatchlistItemRead])
def list_watchlist_items(watchlist_id: int, db: Session = Depends(get_db)):
    watchlist = db.get(Watchlist, watchlist_id)
    if watchlist is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return db.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == watchlist_id).order_by(WatchlistItem.id.desc())
    ).scalars().all()


@router.post("/watchlists/{watchlist_id}/items", response_model=WatchlistItemRead)
def add_watchlist_item(watchlist_id: int, payload: WatchlistItemCreate, db: Session = Depends(get_db)):
    watchlist = db.get(Watchlist, watchlist_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if watchlist is None or symbol is None:
        raise HTTPException(status_code=404, detail="Watchlist or symbol not found")

    existing = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.symbol_id == payload.symbol_id,
        )
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Symbol already in watchlist")

    item = WatchlistItem(watchlist_id=watchlist_id, symbol_id=payload.symbol_id, note=payload.note)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/watchlists/{watchlist_id}/items/{symbol_id}")
def delete_watchlist_item(watchlist_id: int, symbol_id: int, db: Session = Depends(get_db)):
    item = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.symbol_id == symbol_id,
        )
    ).scalars().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    db.delete(item)
    db.commit()
    return {"deleted": True}

