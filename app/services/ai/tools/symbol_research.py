"""get_symbol_research 工具：返回标的研究数据。"""
from __future__ import annotations

import logging

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack

logger = logging.getLogger(__name__)


def get_symbol_research(db: Session, context: ContextPack) -> dict:
    """返回标的研究数据：基本信息、行情、评分、因子。

    依赖 context.references.symbol_id。

    Returns:
        {
            "status": "ok",
            "data": {
                "symbol": {...},  # 基本信息
                "latest_bar": {...},  # 最新行情
                "latest_score": {...},  # 最新评分
                "news": [...],  # 相关新闻（标记 is_untrusted）
            }
        }
        或 {"status": "no_data", "message": "..."}
    """
    symbol_id = context.references.get("symbol_id")
    if symbol_id is None:
        return {"status": "no_data", "message": "未指定 symbol_id"}

    # 基本信息
    symbol_info: dict | None = None
    try:
        from app.models.symbol import Symbol
        sym = db.execute(
            select(Symbol).where(Symbol.id == symbol_id)
        ).scalars().first()
        if sym is not None:
            symbol_info = {
                "id": sym.id,
                "symbol": sym.symbol,
                "name": sym.name,
                "asset_type": sym.asset_type,
                "market": sym.market,
                "board": sym.board,
                "industry": sym.industry,
                "theme": sym.theme,
                "is_st": bool(sym.is_st),
                "is_active": bool(sym.is_active),
                "listed_at": sym.listed_at.isoformat() if sym.listed_at else None,
            }
    except Exception as exc:
        logger.warning("get_symbol_research symbol query failed: %s", exc)

    if symbol_info is None:
        return {"status": "no_data", "message": f"标的 symbol_id={symbol_id} 不存在"}

    # 最新行情
    latest_bar: dict | None = None
    try:
        from app.models.daily_bar import DailyBar
        bar = db.execute(
            select(DailyBar)
            .where(DailyBar.symbol_id == symbol_id)
            .order_by(desc(DailyBar.trade_date))
        ).scalars().first()
        if bar is not None:
            latest_bar = {
                "trade_date": bar.trade_date.isoformat() if bar.trade_date else None,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume) if bar.volume is not None else None,
                "amount": float(bar.amount) if bar.amount is not None else None,
                "turnover_rate": float(bar.turnover_rate) if bar.turnover_rate is not None else None,
            }
    except Exception as exc:
        logger.warning("get_symbol_research bar query failed: %s", exc)

    # 最新评分
    latest_score: dict | None = None
    try:
        from app.models.score import Score
        score = db.execute(
            select(Score)
            .where(Score.symbol_id == symbol_id)
            .order_by(desc(Score.trade_date))
        ).scalars().first()
        if score is not None:
            latest_score = {
                "trade_date": score.trade_date.isoformat() if score.trade_date else None,
                "quality_score": float(score.quality_score) if score.quality_score is not None else None,
                "quality_grade": score.quality_grade,
                "timing_score": float(score.timing_score) if score.timing_score is not None else None,
                "stage": score.stage,
                "action": score.action,
                "priority_score": float(score.priority_score) if score.priority_score is not None else None,
                "data_credibility": float(score.data_credibility) if score.data_credibility is not None else None,
                "weight_mode": score.weight_mode,
                "scoring_preset_name": score.scoring_preset_name,
            }
    except Exception as exc:
        logger.warning("get_symbol_research score query failed: %s", exc)

    # 相关新闻（标记不可信）
    news_items: list[dict] = []
    try:
        from app.models.news_event import NewsEvent
        rows = db.execute(
            select(NewsEvent)
            .where(NewsEvent.symbol_id == symbol_id)
            .order_by(desc(NewsEvent.published_at).nullslast())
            .limit(5)
        ).scalars().all()
        for n in rows:
            news_items.append({
                "title": n.title,
                "source": n.source,
                "published_at": n.published_at.isoformat() if n.published_at else None,
                "sentiment": n.sentiment,
                "risk_level": n.risk_level,
                "is_untrusted": True,  # 关键：新闻不可信
            })
    except Exception as exc:
        logger.warning("get_symbol_research news query failed: %s", exc)

    return {
        "status": "ok",
        "data": {
            "symbol": symbol_info,
            "latest_bar": latest_bar,
            "latest_score": latest_score,
            "news": news_items,
        },
    }


__all__ = ["get_symbol_research"]
