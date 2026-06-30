from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.async_utils import run_sync
from app.db.session import get_db
from app.models.market_event import IMPACT_SCOPE_CHOICES
from app.schemas.market_event import (
    MarketEventCollectRequest,
    MarketEventCollectResponse,
    MarketEventCreate,
    MarketEventListResponse,
    MarketEventRead,
    MarketEventUpdate,
)
from app.services.market_events import (
    collect_market_events,
    create_market_event,
    delete_market_event,
    get_market_event,
    list_market_events,
    update_market_event,
)

router = APIRouter()


@router.get("/market-events", response_model=MarketEventListResponse)
def list_events(
    impact_scope: str | None = Query(default=None),
    importance_level_min: int | None = Query(default=None, ge=1, le=5),
    importance_level_max: int | None = Query(default=None, ge=1, le=5),
    affected_market: str | None = Query(default=None),
    sentiment: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    is_manual: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="published_at"),
    db: Session = Depends(get_db),
):
    return list_market_events(
        db,
        impact_scope=impact_scope,
        importance_level_min=importance_level_min,
        importance_level_max=importance_level_max,
        affected_market=affected_market,
        sentiment=sentiment,
        date_from=date_from,
        date_to=date_to,
        is_manual=is_manual,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
    )


@router.get("/market-events/scopes", response_model=list[str])
def list_scopes():
    return IMPACT_SCOPE_CHOICES


@router.get("/market-events/{event_id}", response_model=MarketEventRead)
def get_event(event_id: int, db: Session = Depends(get_db)):
    event = get_market_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Market event not found")
    from app.services.market_events import _event_to_read
    return _event_to_read(event)


@router.post("/market-events", response_model=MarketEventRead)
def create_event(payload: MarketEventCreate, db: Session = Depends(get_db)):
    event = create_market_event(db, payload)
    from app.services.market_events import _event_to_read
    return _event_to_read(event)


@router.put("/market-events/{event_id}", response_model=MarketEventRead)
def update_event(event_id: int, payload: MarketEventUpdate, db: Session = Depends(get_db)):
    event = update_market_event(db, event_id, payload)
    if event is None:
        raise HTTPException(status_code=404, detail="Market event not found")
    from app.services.market_events import _event_to_read
    return _event_to_read(event)


@router.delete("/market-events/{event_id}")
def delete_event(event_id: int, db: Session = Depends(get_db)):
    ok = delete_market_event(db, event_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Market event not found")
    return {"detail": "ok"}


@router.post("/market-events/collect", response_model=MarketEventCollectResponse)
async def trigger_collect(payload: MarketEventCollectRequest, db: Session = Depends(get_db)):
    return await run_sync(collect_market_events, db, payload)