"""get_data_health 工具：返回数据健康状态。"""
from __future__ import annotations

import logging
from datetime import date, timedelta, timezone

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack

logger = logging.getLogger(__name__)


def _utcnow_naive():
    from datetime import datetime
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_data_health(db: Session, context: ContextPack) -> dict:
    """返回数据健康状态：最新数据日期、缺失项、过期项。

    Returns:
        {
            "status": "ok",
            "data": {
                "latest_trade_date": str|None,
                "total_symbols": int,
                "covered_symbols": int,
                "coverage_pct": float,
                "stale_symbol_count": int,  # 超过 7 天未更新行情的标的
                "missing_items": [...],
            }
        }
        或 {"status": "no_data", "message": "..."}
    """
    try:
        from app.models.daily_bar import DailyBar
        from app.models.symbol import Symbol
    except Exception as exc:
        logger.warning("get_data_health model import failed: %s", exc)
        return {"status": "no_data", "message": "数据模型不可用"}

    try:
        total_symbols = db.execute(
            select(func.count(Symbol.id)).where(Symbol.is_active == 1)
        ).scalar_one()
    except Exception as exc:
        logger.warning("get_data_health total_symbols failed: %s", exc)
        return {"status": "no_data", "message": "标的库查询失败"}

    try:
        latest_trade_date = db.execute(
            select(func.max(DailyBar.trade_date))
        ).scalar_one_or_none()
    except Exception as exc:
        logger.warning("get_data_health latest_trade_date failed: %s", exc)
        latest_trade_date = None

    try:
        # 子查询：每个 symbol 的最新行情日期
        latest_bar_subq = (
            select(
                DailyBar.symbol_id,
                func.max(DailyBar.trade_date).label("latest_trade_date"),
            )
            .group_by(DailyBar.symbol_id)
            .subquery()
        )
        covered_symbols = db.execute(
            select(func.count(Symbol.id))
            .join(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1)
        ).scalar_one()
    except Exception as exc:
        logger.warning("get_data_health covered_symbols failed: %s", exc)
        covered_symbols = 0

    coverage_pct = (
        round((covered_symbols / total_symbols) * 100, 2)
        if total_symbols else 0.0
    )

    # 过期标的（超过 7 天未更新）
    stale_cutoff = (_utcnow_naive().date() - timedelta(days=7))
    stale_symbol_count = 0
    try:
        latest_bar_subq2 = (
            select(
                DailyBar.symbol_id,
                func.max(DailyBar.trade_date).label("latest_trade_date"),
            )
            .group_by(DailyBar.symbol_id)
            .subquery()
        )
        stale_symbol_count = db.execute(
            select(func.count(Symbol.id))
            .join(latest_bar_subq2, latest_bar_subq2.c.symbol_id == Symbol.id)
            .where(
                Symbol.is_active == 1,
                latest_bar_subq2.c.latest_trade_date < stale_cutoff,
            )
        ).scalar_one()
    except Exception as exc:
        logger.debug("get_data_health stale_symbol_count failed: %s", exc)

    missing_items = list(context.data_status.get("missing_items", []))

    if total_symbols == 0 and latest_trade_date is None:
        return {
            "status": "no_data",
            "message": "尚无任何标的或行情数据",
        }

    return {
        "status": "ok",
        "data": {
            "latest_trade_date": (
                latest_trade_date.isoformat()
                if hasattr(latest_trade_date, "isoformat")
                else (str(latest_trade_date) if latest_trade_date else None)
            ),
            "total_symbols": int(total_symbols),
            "covered_symbols": int(covered_symbols),
            "coverage_pct": float(coverage_pct),
            "stale_symbol_count": int(stale_symbol_count),
            "missing_items": missing_items,
            "data_cutoff": context.data_status.get("data_cutoff"),
            "credibility": context.data_status.get("credibility", "high"),
        },
    }


__all__ = ["get_data_health"]
