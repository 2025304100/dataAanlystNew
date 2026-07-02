from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.async_utils import run_sync
from app.db.session import get_db
from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.schemas.async_task import AsyncTaskRead, MarketDataSyncCreate
from app.schemas.market_data import (
    DailyBarImportRequest,
    DailyBarRead,
    HistoryInitializationRequest,
    HistoryInitializationRetryRequest,
    HistoryInitializationStatus,
    MarketDataRepairRequest,
    MarketDataUpdateRequest,
)
from app.services.analysis import calculate_symbol_score
from app.services.async_tasks import cancel_async_task, get_async_task, list_async_tasks
from app.services.market_data import (
    cancel_history_initialization_task,
    cleanup_history_records,
    create_history_initialization_task,
    get_history_initialization_status,
    retry_history_initialization_failed_items,
    sync_market_data,
    sync_symbol_daily_bars,
)
from app.services.market_data_sync_task import create_market_data_sync_task


router = APIRouter()


@router.post("/market-data/update")
async def trigger_market_data_update(payload: MarketDataUpdateRequest, db: Session = Depends(get_db)) -> dict:
    try:
        result = await run_sync(
            sync_market_data,
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


@router.post("/market-data/sync-tasks", response_model=AsyncTaskRead)
def create_sync_task(payload: MarketDataSyncCreate) -> dict:
    return create_market_data_sync_task(payload)


@router.get("/market-data/sync-tasks", response_model=list[AsyncTaskRead])
def list_sync_tasks(limit: int = Query(10, ge=1, le=50)) -> list[dict]:
    return [task.model_dump() for task in list_async_tasks(task_type="market_data_sync", limit=limit)]


@router.get("/market-data/sync-tasks/{task_id}", response_model=AsyncTaskRead)
def get_sync_task(task_id: str) -> dict:
    task = get_async_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Sync task not found")
    return task.model_dump()


@router.post("/market-data/sync-tasks/{task_id}/cancel", response_model=AsyncTaskRead)
def cancel_sync_task(task_id: str) -> dict:
    try:
        return cancel_async_task(task_id).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/market-data/initialize-history", response_model=HistoryInitializationStatus)
def initialize_history_data(payload: HistoryInitializationRequest) -> dict:
    try:
        return create_history_initialization_task(
            preset=payload.preset,
            adjust=payload.adjust,
            asset_types=payload.asset_types,
            symbol_ids=payload.symbol_ids,
            repair_mode=payload.repair_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/market-data/initialize-history/status", response_model=HistoryInitializationStatus)
def get_initialize_history_status() -> dict:
    return get_history_initialization_status()


@router.post("/market-data/initialize-history/cancel", response_model=HistoryInitializationStatus)
def cancel_initialize_history() -> dict:
    try:
        return cancel_history_initialization_task()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc



@router.post("/market-data/initialize-history/retry-failed", response_model=HistoryInitializationStatus)
def retry_initialize_history_failed(payload: HistoryInitializationRetryRequest) -> dict:
    try:
        return retry_history_initialization_failed_items(payload.task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
@router.post("/market-data/initialize-history/cleanup")
def cleanup_initialize_history(keep: int = Query(5, ge=1, le=50)) -> dict:
    return cleanup_history_records(keep=keep)


@router.post("/market-data/symbols/{symbol_id}/repair")
async def repair_symbol_market_data(symbol_id: int, payload: MarketDataRepairRequest, db: Session = Depends(get_db)) -> dict:
    symbol = db.get(Symbol, symbol_id)
    if symbol is None or symbol.is_active != 1:
        raise HTTPException(status_code=404, detail="Symbol not found")

    try:
        result = await run_sync(
            sync_symbol_daily_bars,
            db=db,
            symbol=symbol,
            start_date=payload.start_date,
            end_date=payload.end_date,
            adjust=payload.adjust,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Market data repair failed: {exc}") from exc

    latest_score = None
    if payload.auto_score and result.get("status") == "ok":
        latest_bar = db.execute(
            select(DailyBar)
            .where(DailyBar.symbol_id == symbol.id)
            .order_by(DailyBar.trade_date.desc())
        ).scalars().first()
        if latest_bar is not None:
            score = calculate_symbol_score(db=db, symbol=symbol, trade_date=latest_bar.trade_date)
            latest_score = {
                "trade_date": latest_bar.trade_date,
                "quality_score": score.quality_score,
                "timing_score": score.timing_score,
                "stage": score.stage,
                "action": score.action,
            }

    db.commit()
    return {
        "success": result.get("status") == "ok",
        "message": "Symbol market data repair completed",
        "data": result,
        "latest_score": latest_score,
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
