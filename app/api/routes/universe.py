from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.async_task import AsyncTaskRead
from app.services import universe_sync, universe_sync_task


router = APIRouter()


# 支持的初始化 scope 列表（P0.6：支持分 scope 独立初始化）
_ALL_INIT_SCOPES = ["cn-stock", "cn-etf", "us-stock", "us-etf"]


class UniverseInitRequest(BaseModel):
    """基础数据初始化同步请求。"""

    max_workers: int = Field(default=5, ge=1, le=8, description="并发线程数（1-8）")
    history_days: int = Field(default=365, ge=30, le=3650, description="历史K线天数（近1月=30/近1年=365/近3年=1095/近5年=1825/近10年=3650）")
    scopes: list[str] | None = Field(
        default=None,
        description="要初始化的 scope 列表（cn-stock/cn-etf/us-stock/us-etf），None=全部",
    )
    sync_limit: int = Field(default=0, ge=0, le=10000, description="单次同步标的上限（0=不限，>0 只取前N个未同步标的），用于分段同步")


class UniverseIncrementalRequest(BaseModel):
    """基础数据增量同步请求。"""

    max_workers: int = Field(default=5, ge=1, le=8, description="并发线程数（1-8）")


class UniverseStatsRead(BaseModel):
    """基础数据健康度。"""

    total_symbols: int
    synced_symbols: int
    failed_symbols: int
    total_bars: int
    by_type: dict
    latest_synced_at: str | None = None
    is_empty: bool


@router.post("/universe/initialize", response_model=AsyncTaskRead)
def start_initialize(payload: UniverseInitRequest):
    """触发基础数据初始化同步。

    P0.6：支持通过 scopes 参数指定只初始化部分 scope（如 ["cn-stock","cn-etf"]），
    None 或空列表则初始化全部（cn-stock + cn-etf + us-stock + us-etf）。

    若已有运行中任务则返回该任务，避免重复创建。
    """
    scopes = payload.scopes
    if not scopes:
        scopes = list(_ALL_INIT_SCOPES)
    else:
        # 校验 scope 合法性
        invalid = [s for s in scopes if s not in _ALL_INIT_SCOPES]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的 scope: {invalid}，可选: {_ALL_INIT_SCOPES}",
            )
    return universe_sync_task.start_universe_sync_init(
        max_workers=payload.max_workers,
        history_days=payload.history_days,
        scopes=scopes,
        sync_limit=payload.sync_limit,
    )


@router.get("/universe/initialize/status", response_model=AsyncTaskRead | None)
def get_initialize_status():
    """查询最新的初始化同步任务进度。"""
    return universe_sync_task.get_latest_universe_sync_task()


@router.post("/universe/initialize/cancel", response_model=AsyncTaskRead | None)
def cancel_initialize():
    """取消最新的运行中初始化同步任务。

    已同步的标的不会丢失，重试可继续（断点续传）。
    """
    latest = universe_sync_task.get_latest_universe_sync_task()
    if latest is None:
        raise HTTPException(status_code=404, detail="无初始化同步任务")
    if latest.status not in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"任务已处于终态：{latest.status}")
    result = universe_sync_task.cancel_universe_sync_task(latest.id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


@router.post("/universe/initialize/retry", response_model=AsyncTaskRead)
def retry_initialize(payload: UniverseInitRequest):
    """重试初始化同步（跳过已同步标的，断点续传）。"""
    scopes = payload.scopes
    if not scopes:
        scopes = list(_ALL_INIT_SCOPES)
    else:
        invalid = [s for s in scopes if s not in _ALL_INIT_SCOPES]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的 scope: {invalid}，可选: {_ALL_INIT_SCOPES}",
            )
    return universe_sync_task.retry_universe_sync_init(
        max_workers=payload.max_workers,
        history_days=payload.history_days,
        scopes=scopes,
        sync_limit=payload.sync_limit,
    )


@router.get("/universe/stats")
def get_stats(db: Session = Depends(get_db)):
    """基础数据健康度统计（覆盖率、新鲜度）。"""
    return universe_sync.get_universe_stats(db)


# ── P2：定时增量同步 API ──


@router.post("/universe/incremental-sync", response_model=AsyncTaskRead)
def start_incremental_sync(payload: UniverseIncrementalRequest):
    """触发基础数据增量同步（只同步 last_bar_date < today 的标的）。

    适合每日定时执行，保持数据新鲜。
    若已有运行中的同步任务（init 或 incremental）则返回该任务。
    """
    return universe_sync_task.start_universe_incremental_sync(
        max_workers=payload.max_workers,
    )


@router.get("/universe/incremental-sync/status", response_model=AsyncTaskRead | None)
def get_incremental_sync_status():
    """查询最新的增量同步任务进度。"""
    return universe_sync_task.get_latest_universe_incremental_task()


@router.post("/universe/incremental-sync/cancel", response_model=AsyncTaskRead | None)
def cancel_incremental_sync():
    """取消最新的运行中增量同步任务。"""
    latest = universe_sync_task.get_latest_universe_incremental_task()
    if latest is None:
        raise HTTPException(status_code=404, detail="无增量同步任务")
    if latest.status not in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"任务已处于终态：{latest.status}")
    result = universe_sync_task.cancel_universe_incremental_task(latest.id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


# ── 历史回补 API ──


@router.post("/universe/backfill", response_model=AsyncTaskRead)
def start_backfill(payload: UniverseInitRequest):
    """触发历史回补（对已同步标的强制按新 history_days 重新拉取K线）。

    适用于扩展历史范围（如5年→10年）或修正历史数据缺口。
    已有K线会被覆盖更新（upsert），不会丢失数据。

    若已有运行中的同步任务（init/incremental/backfill）则返回该任务。
    """
    scopes = payload.scopes
    if not scopes:
        scopes = list(_ALL_INIT_SCOPES)
    else:
        invalid = [s for s in scopes if s not in _ALL_INIT_SCOPES]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的 scope: {invalid}，可选: {_ALL_INIT_SCOPES}",
            )
    return universe_sync_task.start_universe_backfill(
        max_workers=payload.max_workers,
        history_days=payload.history_days,
        scopes=scopes,
        sync_limit=payload.sync_limit,
    )


@router.get("/universe/backfill/status", response_model=AsyncTaskRead | None)
def get_backfill_status():
    """查询最新的历史回补任务进度。"""
    return universe_sync_task.get_latest_universe_backfill_task()


@router.post("/universe/backfill/cancel", response_model=AsyncTaskRead | None)
def cancel_backfill():
    """取消最新的运行中历史回补任务。"""
    latest = universe_sync_task.get_latest_universe_backfill_task()
    if latest is None:
        raise HTTPException(status_code=404, detail="无历史回补任务")
    if latest.status not in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"任务已处于终态：{latest.status}")
    result = universe_sync_task.cancel_universe_backfill_task(latest.id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result
