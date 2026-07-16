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

import logging
import time
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.symbol import Symbol

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


def _resolve_symbols(db: Session, source: Literal["watchlist", "positions", "all"], asset_type: str | None) -> list[Symbol]:
    """解析目标 symbol 列表。"""
    if source == "all":
        stmt = select(Symbol).where(Symbol.is_active == 1)
        if asset_type:
            stmt = stmt.where(Symbol.asset_type == asset_type)
        return list(db.execute(stmt).scalars().all())
    if source == "watchlist":
        from app.models.watchlist import WatchlistItem
        stmt = (
            select(Symbol)
            .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1)
        )
        if asset_type:
            stmt = stmt.where(Symbol.asset_type == asset_type)
        return list(db.execute(stmt).scalars().all())
    if source == "positions":
        from app.models.portfolio import Position
        stmt = (
            select(Symbol)
            .join(Position, Position.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1)
        )
        if asset_type:
            stmt = stmt.where(Symbol.asset_type == asset_type)
        return list(db.execute(stmt).scalars().all())
    return []


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
