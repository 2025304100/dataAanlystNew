from __future__ import annotations

import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.index_price import IndexPrice
from app.schemas.async_task import AsyncTaskRead
from app.services import index_data, index_prices_sync_task, universe_sync, universe_sync_task


router = APIRouter()


# 支持的初始化 scope 列表（P0.6：支持分 scope 独立初始化）
_ALL_INIT_SCOPES = ["cn-stock", "cn-etf", "us-stock", "us-etf"]


def _normalize_scopes(scopes: list[str] | None) -> list[str]:
    if not scopes:
        return list(_ALL_INIT_SCOPES)
    invalid = [s for s in scopes if s not in _ALL_INIT_SCOPES]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 scope: {invalid}，可选: {_ALL_INIT_SCOPES}",
        )
    return scopes


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
    scopes: list[str] | None = Field(
        default=None,
        description="要增量同步的 scope 列表（cn-stock/cn-etf/us-stock/us-etf），None=全部",
    )


class UniverseRangeRepairRequest(BaseModel):
    """Chunked range-repair request for already-synced symbols."""

    max_workers: int = Field(default=5, ge=1, le=8, description="worker count (1-8)")
    history_days: int = Field(default=365, ge=30, le=3650, description="recent range size in days")
    chunk_days: int = Field(default=90, ge=7, le=365, description="chunk size for each re-fetch window")
    scopes: list[str] | None = Field(
        default=None,
        description="target scopes (cn-stock/cn-etf/us-stock/us-etf), None means all",
    )
    sync_limit: int = Field(default=0, ge=0, le=10000, description="max symbols to process in one repair run")

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
    scopes = _normalize_scopes(payload.scopes)
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
    scopes = _normalize_scopes(payload.scopes)
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
    """触发指定范围内的基础数据增量同步。

    适合每日定时执行，保持数据新鲜。
    若已有运行中的同步任务（init 或 incremental）则返回该任务。
    """
    scopes = _normalize_scopes(payload.scopes)
    return universe_sync_task.start_universe_incremental_sync(
        max_workers=payload.max_workers,
        scopes=scopes,
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


# ── 智能同步 API ──


@router.post("/universe/smart-sync", response_model=AsyncTaskRead)
def start_smart_sync(payload: UniverseInitRequest):
    """触发智能同步。

    自动按当前数据覆盖情况完成：
    - 刷新标的列表
    - 初始化未同步标的
    - 历史左侧补缺
    - 近期右侧增量续刷
    """
    scopes = _normalize_scopes(payload.scopes)
    return universe_sync_task.start_universe_smart_sync(
        max_workers=payload.max_workers,
        history_days=payload.history_days,
        scopes=scopes,
        sync_limit=payload.sync_limit,
    )


@router.get("/universe/smart-sync/status", response_model=AsyncTaskRead | None)
def get_smart_sync_status():
    """查询最新的智能同步任务进度。"""
    return universe_sync_task.get_latest_universe_smart_task()


@router.post("/universe/smart-sync/cancel", response_model=AsyncTaskRead | None)
def cancel_smart_sync():
    """取消最新的运行中智能同步任务。"""
    latest = universe_sync_task.get_latest_universe_smart_task()
    if latest is None:
        raise HTTPException(status_code=404, detail="无智能同步任务")
    if latest.status not in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"任务已处于终态：{latest.status}")
    result = universe_sync_task.cancel_universe_smart_task(latest.id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


# ── 历史回补 API ──


# —— Range Repair API ——


@router.post("/universe/range-repair", response_model=AsyncTaskRead)
def start_range_repair(payload: UniverseRangeRepairRequest):
    """Trigger chunked range repair for mid-history gaps within a recent window."""
    scopes = _normalize_scopes(payload.scopes)
    return universe_sync_task.start_universe_range_repair(
        max_workers=payload.max_workers,
        history_days=payload.history_days,
        chunk_days=payload.chunk_days,
        scopes=scopes,
        sync_limit=payload.sync_limit,
    )


@router.get("/universe/range-repair/status", response_model=AsyncTaskRead | None)
def get_range_repair_status():
    """Query the latest range-repair task."""
    return universe_sync_task.get_latest_universe_range_repair_task()


@router.post("/universe/range-repair/cancel", response_model=AsyncTaskRead | None)
def cancel_range_repair():
    """Cancel the latest running range-repair task."""
    latest = universe_sync_task.get_latest_universe_range_repair_task()
    if latest is None:
        raise HTTPException(status_code=404, detail="No range repair task")
    if latest.status not in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"Task already ended: {latest.status}")
    result = universe_sync_task.cancel_universe_range_repair_task(latest.id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result

@router.post("/universe/backfill", response_model=AsyncTaskRead)
def start_backfill(payload: UniverseInitRequest):
    """触发历史回补（对已同步标的强制按新 history_days 重新拉取K线）。

    适用于扩展历史范围（如5年→10年）或修正历史数据缺口。
    已有K线会被覆盖更新（upsert），不会丢失数据。

    若已有运行中的同步任务（init/incremental/backfill）则返回该任务。
    """
    scopes = _normalize_scopes(payload.scopes)
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


# ═══════════════════════════════════════════════════════════════════════════
# 指数日线数据同步（回测基准曲线数据源：index_prices 表）
# 集成到"设置 - 基础数据"页面，与 Universe 股票数据打通
# ═══════════════════════════════════════════════════════════════════════════

# 5 大基准指数：与 portfolio_backtest._BENCHMARK_NAME_TO_SYMBOL 保持一致
# 注：此处显式定义，避免循环 import
_BENCHMARK_DEFAULTS: list[dict[str, str]] = [
    {"name": "沪深300", "symbol": "000300"},
    {"name": "中证500", "symbol": "000905"},
    {"name": "创业板指", "symbol": "399006"},
    {"name": "上证50",  "symbol": "000016"},
    {"name": "科创50",  "symbol": "000688"},
]


class IndexPriceSyncRequest(BaseModel):
    """单/多指数日线同步请求。"""
    symbols: list[str] | None = Field(
        default=None,
        description="要同步的指数代码列表，如 [\"000300\",\"399006\"]；None 表示同步全部 5 大基准指数",
    )
    history_days: int = Field(
        default=1825, ge=30, le=7300,
        description="历史K线天数（默认5年=1825；近1年=365；近3年=1095；近10年=3650）",
    )
    end_date: date | None = Field(default=None, description="结束日期（含），None=今天")


class IndexPriceSyncItem(BaseModel):
    symbol: str
    name: str
    received: int = 0
    written: int = 0
    skipped: int = 0
    first_date: date | None = None
    last_date: date | None = None
    error: str | None = None


class IndexPriceSyncResponse(BaseModel):
    """指数日线同步结果。"""
    total: int
    success: int
    failed: int
    items: list[IndexPriceSyncItem]


class IndexPriceStatusItem(BaseModel):
    """单个指数的数据健康度。"""
    symbol: str
    name: str
    bar_count: int
    first_date: date | None = None
    last_date: date | None = None
    freshness_days: int | None = None  # 距今天数（None=无数据）
    # 线性度检测（0=完全直线，越大越非线性；>0.001 视为有真实波动）
    linearity_dev_pct: float | None = None


class IndexPriceStatusResponse(BaseModel):
    items: list[IndexPriceStatusItem]


def _default_symbols(symbols: list[str] | None) -> list[str]:
    if symbols and len(symbols) > 0:
        return list(symbols)
    return [b["symbol"] for b in _BENCHMARK_DEFAULTS]


def _name_of(symbol: str) -> str:
    for b in _BENCHMARK_DEFAULTS:
        if b["symbol"] == symbol:
            return b["name"]
    return symbol


def _linearity_dev(db: Session, symbol: str) -> float | None:
    """计算基准指数偏离直线的程度（百分比）。

    - 有数据且有波动 → 返回偏离度（如 11.26 表示 11.26%）
    - 无数据或数据不足 → None
    - 全是一条直线（极端case）→ 接近 0
    """
    bars = index_data.list_index_prices(db, symbol, limit=1000)
    if len(bars) < 5:
        return None
    closes = [float(b.close) for b in bars if b.close is not None]
    if len(closes) < 5:
        return None
    n = len(closes)
    first, last = closes[0], closes[-1]
    if abs(first) < 1e-9:
        return None
    max_dev = 0.0
    for i in range(n):
        expected = first + (last - first) * (i / (n - 1))
        dev = abs(closes[i] - expected) / abs(expected)
        if dev > max_dev:
            max_dev = dev
    return round(max_dev * 100, 3)


# ──────────────── 列表查询 ────────────────

@router.get("/index-prices")
def list_index_prices_endpoint(
    symbol: str,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 2000,
    db: Session = Depends(get_db),
):
    """查询单个指数的日线数据（公开列表）。"""
    bars = index_data.list_index_prices(
        db, symbol, start_date=start_date, end_date=end_date, limit=limit,
    )
    items = [
        {
            "symbol": b.symbol,
            "trade_date": b.trade_date,
            "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "volume": b.volume, "amount": b.amount,
            "source": b.source,
        }
        for b in bars
    ]
    return {"items": items, "total": len(items)}


# ──────────────── 状态查询 ────────────────

@router.get("/index-prices/status", response_model=IndexPriceStatusResponse)
def get_index_prices_status(
    symbols: str | None = None,
    db: Session = Depends(get_db),
):
    """查询 5 大基准指数（或指定）的健康度。

    Query 参数 `symbols`：可选，逗号分隔，如 "000300,399006"；空=默认5个
    """
    target_symbols: list[str]
    if symbols:
        target_symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        # 默认：默认 5 基准 + DB 中所有已存在的指数（保留之前用户/自定义同步过的）
        from sqlalchemy import distinct as sa_distinct
        existing_syms = db.execute(select(sa_distinct(IndexPrice.symbol))).scalars().all()
        existing_syms = [s for s in existing_syms if s]
        merged: list[str] = [b["symbol"] for b in _BENCHMARK_DEFAULTS]
        for s in existing_syms:
            if s not in merged:
                merged.append(s)
        target_symbols = merged

    today = date.today()
    items: list[IndexPriceStatusItem] = []
    for sym in target_symbols:
        # count / min / max trade_date
        stmt = select(
            func.count(IndexPrice.id),
            func.min(IndexPrice.trade_date),
            func.max(IndexPrice.trade_date),
        ).where(IndexPrice.symbol == sym)
        count_, first_, last_ = db.execute(stmt).one()
        count_ = int(count_ or 0)
        freshness = (today - last_).days if last_ else None
        dev = _linearity_dev(db, sym)
        items.append(IndexPriceStatusItem(
            symbol=sym,
            name=_name_of(sym),
            bar_count=count_,
            first_date=first_,
            last_date=last_,
            freshness_days=freshness,
            linearity_dev_pct=dev,
        ))
    return IndexPriceStatusResponse(items=items)


# ──────────────── 异步提交（心跳轮询） ────────────────

@router.post("/index-prices/sync", response_model=AsyncTaskRead)
async def sync_index_prices(
    payload: IndexPriceSyncRequest,
):
    """异步提交指数日线同步 → 返回 task_id，前端心跳轮询进度。

    之前同步阻塞 HTTP，指数多时容易超时返回"同步失败"但后端还在跑；
    现改为后台 daemon 线程执行（index_prices_sync_task），
    通过 GET /index-prices/sync-tasks/{task_id} 2s 轮询 percent/status。

    路由使用 async def：避免与 universe_sync / market_data_sync 等同步 def 路由
    抢 anyio 线程池的槽位（默认 40 线程，被大量重任务占满时新请求排队会超 20s 前端 timeout）。
    create_index_prices_sync_task 本身 DB 写入轻量 (<30ms)，用 to_thread 抛到
    独立 worker 中，eventloop 不被阻塞。

    - 默认同步 5 大基准；也可 symbols 指定（支持自定义指数）
    - history_days: 回补范围，默认 1825（5 年）
    """
    return await asyncio.to_thread(
        index_prices_sync_task.create_index_prices_sync_task,
        symbols=payload.symbols,
        history_days=int(payload.history_days),
        end_date=payload.end_date,
    )


@router.get("/index-prices/sync-tasks/{task_id}", response_model=AsyncTaskRead)
async def get_index_prices_sync_task(task_id: str):
    """指数同步任务进度查询（心跳轮询用）。

    返回 AsyncTaskRead：status / percent / message / total / processed / ok_count / failed_count
    终态 done 时 result={ total, success, failed, items:[{symbol,written,...error}] }
    与原先同步接口 IndexPriceSyncResponse 结构一致，前端直接复用更新行状态。

    async def + to_thread 使心跳不占用 anyio threadpool 槽，避免被其它重任务堵死。
    """
    task = await asyncio.to_thread(index_prices_sync_task.get_index_prices_sync_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Index sync task not found")
    return task


# ──────────────── 便捷：同步全部 5 大基准（默认5年） ────────────────

@router.post("/index-prices/sync-all-benchmarks", response_model=AsyncTaskRead)
async def sync_all_benchmarks():
    """一键异步提交 5 大基准指数同步（默认最近5年）。

    专为"设置 - 基础数据"面板的"一键同步基准"按钮设计。
    返回 AsyncTaskRead，前端心跳轮询 /index-prices/sync-tasks/{task_id}。

    async def + to_thread：规避 anyio threadpool 饥饿导致前端 20s 超时。
    """
    return await asyncio.to_thread(
        index_prices_sync_task.create_index_prices_sync_task,
        symbols=None,
        history_days=1825,
    )
