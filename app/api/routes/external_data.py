"""P2：外部数据同步 API。

提供七类外部数据的手动批量同步端点：
1. /api/external-data/fundamental/sync - 股票估值（PE/PB/市值/行业分位）
2. /api/external-data/financial-reports/sync - 财报与公告日历史
3. /api/external-data/lhb-institution/sync - 龙虎榜机构席位净买额
4. /api/external-data/hot-rank/sync - 东方财富当前人气榜前100名
5. /api/external-data/tail-proxy/sync - 候选池尾盘分钟量价代理
6. /api/external-data/capital-flow/sync - 资金流（主力/超大单/北向）
7. /api/external-data/etf-indicators/sync - ETF 特有指标（溢价折价/规模/份额）

同步为同步阻塞操作（适合 watchlist/positions 等小批量场景）；
全市场同步建议通过 opportunity discovery 任务的按需拉取机制完成。
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.symbol import Symbol
from app.schemas.async_task import AsyncTaskRead
from app.services.external_data_sync_task import (
    ExternalDataset,
    ExternalSyncMode,
    build_external_sync_plan,
    cancel_external_data_sync,
    get_external_sync_capabilities,
    get_external_data_coverage,
    get_external_data_gaps,
    get_external_data_overview,
    get_external_sync_partitions,
    get_external_sync_task,
    resolve_external_symbols,
    retry_external_data_sync,
    start_external_data_sync,
    start_external_gap_repair,
)
from app.services.data_quality import (
    capture_field_quality_snapshots,
    list_field_quality_history,
    list_latest_field_quality,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 批量同步时每个 symbol 之间的间隔（秒），避免触发东方财富 QPS 限制
_SYNC_THROTTLE_SECONDS = 0.3


class SyncResult(BaseModel):
    """同步结果。"""
    total: int = 0  # 待同步总数
    success: int = 0  # 成功数
    skipped: int = 0  # 跳过数（数据源无数据或非目标资产）
    failed: int = 0  # 失败数
    records: int = 0  # 写入或更新的底层记录数
    errors: list[str] = []  # 前 N 条错误信息


class ExternalSyncTaskCreate(BaseModel):
    """Create an observable external-data synchronization task."""

    dataset: ExternalDataset
    source: Literal["watchlist", "positions", "all"] = "watchlist"
    watchlist_id: int | None = None
    include_northbound: bool = True
    mode: ExternalSyncMode = "incremental"
    start_date: date | None = None
    end_date: date | None = None
    lookback_days: int = Field(default=30, ge=1, le=100)
    limit: int = Field(default=20, ge=1, le=50)
    max_workers: int = Field(default=4, ge=1, le=8)


class ExternalDatasetOverview(BaseModel):
    dataset: ExternalDataset
    records: int
    symbols: int
    latest_date: date | None = None
    last_updated_at: datetime | None = None
    latest_task: AsyncTaskRead | None = None


class ExternalDataOverview(BaseModel):
    datasets: list[ExternalDatasetOverview]
    total_records: int
    covered_symbols: int
    available_datasets: int
    running_tasks: int
    refreshed_at: datetime


class ExternalDataGapItem(BaseModel):
    symbol_id: int
    symbol: str
    trade_date: date


class ExternalDataGapRepairRequest(BaseModel):
    dataset: Literal["fundamental", "financial", "capital_flow"]
    start_date: date
    end_date: date
    gaps: list[ExternalDataGapItem] = Field(min_length=1, max_length=1000)


class TailMinuteImportRequest(BaseModel):
    """Controlled CSV import: symbol,timestamp,open,high,low,close,volume,amount."""

    csv_text: str = Field(min_length=1, max_length=20_000_000)
    filename: str | None = Field(default=None, max_length=160)


def _resolve_symbols(db: Session, source: Literal["watchlist", "positions", "all"], asset_type: str | None) -> list[Symbol]:
    """解析目标 symbol 列表。"""
    return resolve_external_symbols(db, source, asset_type)


@router.get("/external-data/overview", response_model=ExternalDataOverview)
def external_data_overview(db: Session = Depends(get_db)):
    """Return real inventory and latest task status for all external datasets."""
    return get_external_data_overview(db)


@router.get("/external-data/coverage")
def external_data_coverage(db: Session = Depends(get_db)):
    """Return field coverage and readiness from the factor warehouse cache."""
    return get_external_data_coverage(db)


@router.get("/external-data/quality-snapshots")
def latest_external_data_quality_snapshots(db: Session = Depends(get_db)):
    """Return the latest persisted field-quality evidence and failure reasons."""
    return list_latest_field_quality(db)


@router.get("/external-data/quality-snapshots/{field}")
def external_data_quality_history(
    field: str,
    limit: int = Query(30, ge=2, le=180),
    db: Session = Depends(get_db),
):
    """Return a bounded history for a single formula-input field."""
    return list_field_quality_history(db, field, limit=limit)


@router.post("/external-data/quality-snapshots/refresh")
def refresh_external_data_quality_snapshots(db: Session = Depends(get_db)):
    """Capture data quality now; source tables remain strictly read-only."""
    return capture_field_quality_snapshots(db, trigger="manual")


@router.post("/external-data/tail-proxy/import")
def import_tail_proxy_minutes(
    payload: TailMinuteImportRequest,
    db: Session = Depends(get_db),
):
    """Import validated minute bars; it remains candidate/manual data, not market history."""
    from app.services.tail_proxy_import import import_tail_minute_csv

    try:
        return import_tail_minute_csv(
            db, csv_text=payload.csv_text, filename=payload.filename or "upload.csv"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/external-data/gaps")
def external_data_gaps(
    dataset: Literal["fundamental", "financial", "capital_flow"],
    start_date: date,
    end_date: date,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Find missing real external rows using the existing local daily-bar scope."""
    try:
        return get_external_data_gaps(
            db,
            dataset=dataset,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/external-data/gaps/repair", response_model=AsyncTaskRead)
def repair_external_data_gaps(payload: ExternalDataGapRepairRequest):
    """Create a retryable backfill task from selected missing points."""
    try:
        return start_external_gap_repair(
            payload.dataset,
            start_date=payload.start_date,
            end_date=payload.end_date,
            gaps=[item.model_dump() for item in payload.gaps],
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/external-data/sync-tasks", response_model=AsyncTaskRead)
def create_external_data_sync_task(payload: ExternalSyncTaskCreate):
    """Start a background task so callers can poll real synchronization progress."""
    try:
        return start_external_data_sync(
            payload.dataset,
            payload.model_dump(exclude={"dataset"}),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/external-data/sync-capabilities")
def external_data_sync_capabilities():
    """Expose provider history boundaries before users create a sync task."""
    return get_external_sync_capabilities()


@router.post("/external-data/sync-plans/preview")
def preview_external_data_sync_plan(payload: ExternalSyncTaskCreate):
    """Validate range/mode without performing network calls or creating a task."""
    try:
        return build_external_sync_plan(
            payload.dataset,
            payload.model_dump(exclude={"dataset"}),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/external-data/sync-tasks/{task_id}", response_model=AsyncTaskRead)
def external_data_sync_task_status(task_id: str):
    task = get_external_sync_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="External-data sync task not found")
    return task


@router.get("/external-data/sync-tasks/{task_id}/partitions")
def external_data_sync_task_partitions(task_id: str):
    """Inspect frozen range, partition outcomes, attempts, and errors."""
    result = get_external_sync_partitions(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="External-data sync task not found")
    return result


@router.post("/external-data/sync-tasks/{task_id}/cancel", response_model=AsyncTaskRead)
def cancel_external_data_sync_task(task_id: str):
    """Cancel an external task; completed rows remain available and retryable."""
    try:
        return cancel_external_data_sync(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/external-data/sync-tasks/{task_id}/retry", response_model=AsyncTaskRead)
def retry_external_data_sync_task(task_id: str):
    """Resume an interrupted range task after its persisted symbol cursor."""
    try:
        return retry_external_data_sync(task_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/external-data/fundamental/sync", response_model=SyncResult)
def sync_fundamental(
    source: Literal["watchlist", "positions", "all"] = Query("watchlist"),
    db: Session = Depends(get_db),
):
    """同步股票估值数据（PE/PB/市值/行业分位）。

    仅同步 cn-stock；ETF 自动跳过。
    """
    from app.services.fundamental_data import sync_symbol_valuation
    from datetime import date

    symbols = _resolve_symbols(db, source, asset_type="stock")
    result = SyncResult(total=len(symbols))
    today = date.today()
    for i, sym in enumerate(symbols):
        try:
            val = sync_symbol_valuation(db, sym, today)
            if val is None:
                result.skipped += 1
            else:
                result.success += 1
            db.commit()  # 每个 symbol 单独提交，避免中途崩溃丢失已同步数据
        except Exception as exc:
            db.rollback()
            result.failed += 1
            if len(result.errors) < 5:
                result.errors.append(f"{sym.symbol}: {exc}")
            logger.warning("fundamental sync failed for %s: %s", sym.symbol, exc)
        if i < len(symbols) - 1:
            time.sleep(_SYNC_THROTTLE_SECONDS)
    return result


@router.post(
    "/external-data/financial-reports/sync",
    response_model=SyncResult,
)
def sync_financial_reports(
    source: Literal["watchlist", "positions", "all"] = Query("watchlist"),
    db: Session = Depends(get_db),
):
    """同步 A 股财务分析历史并保留同报告期的不同公告版本。"""
    from app.services.financial_data import sync_symbol_financial_reports

    symbols = _resolve_symbols(db, source, asset_type="stock")
    result = SyncResult(total=len(symbols))
    for i, sym in enumerate(symbols):
        try:
            count = sync_symbol_financial_reports(db, sym)
            if count == 0:
                result.skipped += 1
            else:
                result.success += 1
                result.records += count
            db.commit()
        except Exception as exc:
            db.rollback()
            result.failed += 1
            if len(result.errors) < 5:
                result.errors.append(f"{sym.symbol}: {exc}")
            logger.warning(
                "financial report sync failed for %s: %s",
                sym.symbol,
                exc,
            )
        if i < len(symbols) - 1:
            time.sleep(_SYNC_THROTTLE_SECONDS)
    return result


@router.post(
    "/external-data/lhb-institution/sync",
    response_model=SyncResult,
)
def sync_lhb_institution(
    lookback_days: int = Query(30, ge=1, le=31),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """同步一个不超过 31 日的全市场机构席位龙虎榜区间。"""
    from app.services.lhb_data import sync_lhb_institution_trades

    effective_end = end_date or date.today()
    effective_start = effective_end - timedelta(days=lookback_days - 1)
    try:
        summary = sync_lhb_institution_trades(
            db,
            start_date=effective_start,
            end_date=effective_end,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.warning("LHB institution sync failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"LHB institution sync failed: {exc}",
        ) from exc
    return SyncResult(
        total=summary.received,
        success=summary.written,
        skipped=summary.unmatched,
        records=summary.written,
    )


@router.post(
    "/external-data/hot-rank/sync",
    response_model=SyncResult,
)
def sync_hot_rank(db: Session = Depends(get_db)):
    """保存当前人气榜前 100 名；接口不支持历史回填。"""
    from app.services.hot_rank_data import sync_hot_rank_snapshot

    try:
        summary = sync_hot_rank_snapshot(db)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Hot-rank sync failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Hot-rank sync failed: {exc}",
        ) from exc
    return SyncResult(
        total=summary.received,
        success=summary.written,
        skipped=summary.unmatched,
        records=summary.written,
    )


@router.post(
    "/external-data/tail-proxy/sync",
    response_model=SyncResult,
)
def sync_tail_proxy(
    source: Literal["candidates", "watchlist", "positions"] = Query(
        "candidates"
    ),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """同步候选范围的分钟量价代理，不代表 Level-2 大单主买。"""
    from app.services.tail_proxy_data import sync_tail_proxy_snapshots

    try:
        summary = sync_tail_proxy_snapshots(
            db, source=source, limit=limit
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.warning("Tail proxy sync failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Tail proxy sync failed: {exc}",
        ) from exc
    return SyncResult(
        total=summary.total,
        success=summary.written,
        skipped=summary.skipped,
        failed=summary.failed,
        records=summary.written,
        errors=list(summary.errors),
    )


@router.post("/external-data/capital-flow/sync", response_model=SyncResult)
def sync_capital_flow(
    source: Literal["watchlist", "positions", "all"] = Query("watchlist"),
    include_northbound: bool = Query(True),
    db: Session = Depends(get_db),
):
    """同步资金流数据（个股主力净流入 + 北向资金）。

    仅同步 cn-stock；ETF 自动跳过。
    """
    from app.services.capital_flow_data import sync_symbol_capital_flow, sync_northbound_flow
    from datetime import date

    # 北向资金（市场层面，无论 source 都同步）
    if include_northbound:
        try:
            sync_northbound_flow(db, days=30)
            db.commit()
        except Exception as exc:
            logger.warning("northbound sync failed: %s", exc)

    symbols = _resolve_symbols(db, source, asset_type="stock")
    result = SyncResult(total=len(symbols))
    today = date.today()
    for i, sym in enumerate(symbols):
        try:
            flow = sync_symbol_capital_flow(db, sym, today)
            if flow is None:
                result.skipped += 1
            else:
                result.success += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            result.failed += 1
            if len(result.errors) < 5:
                result.errors.append(f"{sym.symbol}: {exc}")
            logger.warning("capital flow sync failed for %s: %s", sym.symbol, exc)
        if i < len(symbols) - 1:
            time.sleep(_SYNC_THROTTLE_SECONDS)
    return result


@router.post("/external-data/etf-indicators/sync", response_model=SyncResult)
def sync_etf_indicators(
    source: Literal["watchlist", "positions", "all"] = Query("watchlist"),
    db: Session = Depends(get_db),
):
    """同步 ETF 特有指标（溢价折价/规模/份额）。

    仅同步 cn-etf；股票自动跳过。
    """
    from app.services.etf_basic_data import sync_etf_indicator
    from datetime import date

    symbols = _resolve_symbols(db, source, asset_type="etf")
    result = SyncResult(total=len(symbols))
    today = date.today()
    for i, sym in enumerate(symbols):
        try:
            ind = sync_etf_indicator(db, sym, today)
            if ind is None:
                result.skipped += 1
            else:
                result.success += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            result.failed += 1
            if len(result.errors) < 5:
                result.errors.append(f"{sym.symbol}: {exc}")
            logger.warning("etf indicator sync failed for %s: %s", sym.symbol, exc)
        if i < len(symbols) - 1:
            time.sleep(_SYNC_THROTTLE_SECONDS)
    return result
