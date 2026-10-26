from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.async_utils import run_sync
from app.db.session import get_db
from app.models.market_event import IMPACT_SCOPE_CHOICES
from app.schemas.market_event import (
    MarketEventCleanupRequest,
    MarketEventCleanupResponse,
    MarketEventCollectRequest,
    MarketEventCollectResponse,
    MarketEventCreate,
    MarketEventHotspotsResponse,
    MarketEventListResponse,
    MarketEventRead,
    MarketEventUpdate,
)
from app.services.market_events import (
    cleanup_expired_market_events,
    collect_market_events,
    create_market_event,
    delete_market_event,
    get_market_event,
    get_market_event_hotspots,
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
    keyword: str | None = Query(default=None, max_length=64),
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
        keyword=keyword,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
    )


@router.get("/market-events/scopes", response_model=list[str])
def list_scopes():
    return IMPACT_SCOPE_CHOICES


# 固定路径必须先于 /market-events/{event_id} 注册，否则会被当作 event_id 参数匹配
# P2-12：板块关键词后台可维护（DB 覆盖内置，调词不发版）
@router.get("/market-events/sector-concepts")
def list_sector_concepts(db: Session = Depends(get_db)):
    from app.services.sector_concepts import list_sector_concept_configs

    return list_sector_concept_configs(db)


@router.put("/market-events/sector-concepts/{sector_id}")
def update_sector_concepts(sector_id: str, payload: dict, db: Session = Depends(get_db)):
    from app.services.sector_concepts import update_sector_concept_config

    keywords = payload.get("keywords")
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        raise HTTPException(status_code=422, detail="keywords must be a list of strings")
    try:
        return update_sector_concept_config(db, sector_id, keywords, updated_by=payload.get("updated_by"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/market-events/collect-runs")
def list_collect_runs_route(limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    from app.services.market_events import list_collect_runs

    runs = list_collect_runs(db, limit=limit)
    import json as _json

    def _parse(raw):
        try:
            return _json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []

    return [
        {
            "id": run.id,
            "trigger_source": run.trigger_source,
            "days": run.days,
            "sources": _parse(run.sources_json),
            "collected": run.collected,
            "skipped_duplicate": run.skipped_duplicate,
            "status": run.status,
            "errors": _parse(run.errors_json),
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }
        for run in runs
    ]


@router.get("/market-events/hotspots", response_model=MarketEventHotspotsResponse)
def get_hotspots(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    return get_market_event_hotspots(db, days=days)


@router.post("/market-events/cleanup", response_model=MarketEventCleanupResponse)
async def cleanup_events(
    payload: MarketEventCleanupRequest | None = None,
    db: Session = Depends(get_db),
):
    keep_days = payload.keep_days if payload is not None else 90
    return await run_sync(cleanup_expired_market_events, db, keep_days=keep_days)


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