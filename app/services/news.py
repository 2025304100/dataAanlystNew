from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

import akshare as ak
import pandas as pd
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.models.news_event import NewsEvent, NewsSnapshot
from app.models.scan import ScanResult, ScanRun
from app.models.symbol import Symbol
from app.models.watchlist import WatchlistItem
from app.schemas.news import NewsEventRead, NewsMacroSummary, NewsSymbolSummary, NewsUpdateRequest


POSITIVE_KEYWORDS = {
    "回购": 8,
    "增持": 7,
    "中标": 7,
    "预增": 7,
    "增长": 5,
    "盈利": 5,
    "扭亏": 6,
    "分红": 4,
    "政策支持": 6,
    "资金流入": 4,
    "突破": 3,
}
NEGATIVE_KEYWORDS = {
    "减持": -7,
    "亏损": -7,
    "下滑": -5,
    "处罚": -8,
    "监管": -6,
    "问询": -5,
    "解禁": -5,
    "下修": -6,
    "诉讼": -8,
}
RISK_KEYWORDS = {
    "立案": -12,
    "退市": -15,
    "财务造假": -15,
    "违规": -10,
    "停牌": -8,
    "暴雷": -15,
    "重大诉讼": -12,
    "风险警示": -12,
}
SOURCE_WEIGHT = {
    "cninfo": 1.0,
    "notice": 0.95,
    "eastmoney-news": 0.72,
    "macro-news": 0.62,
}


def _resolve_symbols(db: Session, payload: NewsUpdateRequest) -> list[Symbol]:
    if payload.scope == "symbols" and payload.symbol_ids:
        return db.execute(select(Symbol).where(Symbol.id.in_(payload.symbol_ids))).scalars().all()

    if payload.scope == "watchlist" and payload.watchlist_id:
        rows = (
            db.execute(
                select(Symbol)
                .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
                .where(WatchlistItem.watchlist_id == payload.watchlist_id)
            )
            .scalars()
            .all()
        )
        return rows

    if payload.scope == "candidates":
        stmt = select(ScanRun).where(ScanRun.status == "done").order_by(desc(ScanRun.id))
        if payload.portfolio_id is not None:
            stmt = stmt.where(ScanRun.portfolio_id == payload.portfolio_id)
        latest_run = db.execute(stmt).scalars().first()
        if latest_run is None:
            return []
        return (
            db.execute(
                select(Symbol)
                .join(ScanResult, ScanResult.symbol_id == Symbol.id)
                .where(ScanResult.scan_run_id == latest_run.id, ScanResult.result_type == "executable")
                .order_by(ScanResult.rank_no.asc())
            )
            .scalars()
            .all()
        )

    if payload.symbol_ids:
        return db.execute(select(Symbol).where(Symbol.id.in_(payload.symbol_ids))).scalars().all()
    return []


def _first_value(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in row and row[key] is not None and not pd.isna(row[key]):
            return row[key]
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().replace(tzinfo=None)


def _decay_weight(published_at: datetime | None, *, half_life_days: float) -> float:
    if published_at is None:
        return 0.65
    age_days = max(0.0, (datetime.utcnow() - published_at).total_seconds() / 86400)
    return round(math.exp(-age_days / max(half_life_days, 0.1)), 4)


def _classify(title: str, source: str, published_at: datetime | None) -> dict:
    text = title or ""
    raw_score = 0.0
    event_type = "news"
    risk_level = "low"

    for keyword, value in POSITIVE_KEYWORDS.items():
        if keyword in text:
            raw_score += value
            event_type = "positive_event"
    for keyword, value in NEGATIVE_KEYWORDS.items():
        if keyword in text:
            raw_score += value
            event_type = "negative_event"
    for keyword, value in RISK_KEYWORDS.items():
        if keyword in text:
            raw_score += value
            event_type = "risk_event"
            risk_level = "high"

    if raw_score > 0:
        sentiment = "positive"
    elif raw_score < 0:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    source_weight = SOURCE_WEIGHT.get(source, 0.6)
    half_life = 12 if source in {"cninfo", "notice"} else 3
    if risk_level == "high":
        half_life = 30
    effective_score = round(max(-20, min(20, raw_score * source_weight * _decay_weight(published_at, half_life_days=half_life))), 2)
    strength = int(min(5, max(1, round(abs(raw_score) / 3)))) if raw_score else 1
    expires_at = None if published_at is None else published_at + timedelta(days=half_life * 2)
    return {
        "event_type": event_type,
        "sentiment": sentiment,
        "strength": strength,
        "raw_score": raw_score,
        "effective_score": effective_score,
        "risk_level": risk_level,
        "expires_at": expires_at,
    }


def _normalize_event(symbol: Symbol | None, row: dict[str, Any], source: str) -> dict | None:
    title = _first_value(row, ["新闻标题", "标题", "公告标题", "title", "内容", "摘要"])
    if not title:
        return None
    published_at = _parse_datetime(_first_value(row, ["发布时间", "时间", "公告日期", "date", "日期"]))
    url = _first_value(row, ["新闻链接", "链接", "公告链接", "url"])
    classified = _classify(str(title), source, published_at)
    return {
        "symbol_id": symbol.id if symbol is not None else None,
        "symbol": symbol.symbol if symbol is not None else None,
        "title": str(title),
        "source": source,
        "url": str(url) if url else None,
        "published_at": published_at,
        "raw_payload": json.dumps(row, ensure_ascii=False, default=str),
        **classified,
    }


def _fetch_symbol_events(symbol: Symbol, days: int) -> list[dict]:
    events: list[dict] = []
    if symbol.market.lower() in {"sh", "sz", "bj"}:
        try:
            frame = ak.stock_news_em(symbol=symbol.symbol)
            for row in frame.head(30).to_dict("records"):
                event = _normalize_event(symbol, row, "eastmoney-news")
                if event is not None:
                    events.append(event)
        except Exception:
            pass

        start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y%m%d")
        end_date = datetime.utcnow().strftime("%Y%m%d")
        try:
            frame = ak.stock_zh_a_disclosure_report_cninfo(
                symbol=symbol.symbol,
                market="沪深京",
                start_date=start_date,
                end_date=end_date,
            )
            for row in frame.head(30).to_dict("records"):
                event = _normalize_event(symbol, row, "cninfo")
                if event is not None:
                    events.append(event)
        except Exception:
            pass
    return _filter_recent(events, days)


def _fetch_macro_events(days: int) -> list[dict]:
    events: list[dict] = []
    for fetcher in (ak.news_cctv, ak.news_economic_baidu, ak.news_report_time_baidu):
        try:
            frame = fetcher()
            for row in frame.head(40).to_dict("records"):
                event = _normalize_event(None, row, "macro-news")
                if event is not None:
                    events.append(event)
        except Exception:
            pass
    return _filter_recent(events, days)


def _filter_recent(events: list[dict], days: int) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    return [event for event in events if event["published_at"] is None or event["published_at"] >= cutoff]


def _upsert_event(db: Session, event: dict) -> NewsEvent:
    existing = (
        db.execute(
            select(NewsEvent).where(
                NewsEvent.symbol_id == event["symbol_id"],
                NewsEvent.title == event["title"],
                NewsEvent.published_at == event["published_at"],
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        existing = NewsEvent()
        db.add(existing)
    for key, value in event.items():
        setattr(existing, key, value)
    db.flush()
    return existing


def _event_read(event: NewsEvent) -> NewsEventRead:
    return NewsEventRead(
        id=event.id,
        symbol_id=event.symbol_id,
        symbol=event.symbol,
        title=event.title,
        source=event.source,
        url=event.url,
        event_type=event.event_type,
        sentiment=event.sentiment,
        strength=event.strength,
        effective_score=event.effective_score,
        risk_level=event.risk_level,
        published_at=event.published_at,
        expires_at=event.expires_at,
    )


def _summarize_symbol(db: Session, payload: NewsUpdateRequest, symbol: Symbol, events: list[NewsEvent]) -> NewsSymbolSummary:
    score = round(max(-20, min(20, sum(event.effective_score for event in events))), 2)
    positive_count = sum(1 for event in events if event.sentiment == "positive")
    negative_count = sum(1 for event in events if event.sentiment == "negative")
    risk_count = sum(1 for event in events if event.risk_level == "high")
    sentiment = "positive" if score > 2 else "negative" if score < -2 else "neutral"
    risk_level = "high" if risk_count else "medium" if negative_count >= 2 else "low"
    confidence = round(min(1.0, 0.35 + len(events) * 0.08 + (0.2 if risk_count else 0)), 2)
    latest = max(events, key=lambda item: item.published_at or item.created_at, default=None)

    snapshot = NewsSnapshot(
        portfolio_id=payload.portfolio_id,
        symbol_id=symbol.id,
        scope=payload.scope,
        symbol_score=score,
        message_score=score,
        sentiment=sentiment,
        risk_level=risk_level,
        confidence=confidence,
        positive_count=positive_count,
        negative_count=negative_count,
        risk_count=risk_count,
        summary=(latest.title if latest is not None else "暂无最近消息"),
    )
    db.add(snapshot)
    return NewsSymbolSummary(
        symbol_id=symbol.id,
        symbol=symbol.symbol,
        name=symbol.name,
        message_score=score,
        sentiment=sentiment,
        risk_level=risk_level,
        confidence=confidence,
        positive_count=positive_count,
        negative_count=negative_count,
        risk_count=risk_count,
        latest_title=latest.title if latest is not None else None,
        events=[_event_read(event) for event in sorted(events, key=lambda item: item.published_at or item.created_at, reverse=True)[:5]],
    )


def _summarize_macro(events: list[NewsEvent]) -> NewsMacroSummary:
    score = round(max(-20, min(20, sum(event.effective_score for event in events))), 2)
    sentiment = "positive" if score > 2 else "negative" if score < -2 else "neutral"
    risk_count = sum(1 for event in events if event.risk_level == "high")
    risk_level = "high" if risk_count else "low"
    latest = sorted(events, key=lambda item: item.published_at or item.created_at, reverse=True)[:3]
    summary = "；".join(event.title for event in latest) if latest else "暂无大环境消息"
    return NewsMacroSummary(
        message_score=score,
        sentiment=sentiment,
        risk_level=risk_level,
        summary=summary,
        events=[_event_read(event) for event in latest],
    )


def _events_for_symbol(db: Session, symbol_id: int, days: int) -> list[NewsEvent]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    return (
        db.execute(
            select(NewsEvent)
            .where(
                NewsEvent.symbol_id == symbol_id,
                or_(NewsEvent.published_at.is_(None), NewsEvent.published_at >= cutoff),
            )
            .order_by(desc(NewsEvent.published_at), desc(NewsEvent.created_at))
            .limit(5)
        )
        .scalars()
        .all()
    )


def _summary_from_snapshot(db: Session, snapshot: NewsSnapshot, symbol: Symbol, days: int) -> NewsSymbolSummary:
    events = _events_for_symbol(db, symbol.id, days)
    latest = events[0] if events else None
    return NewsSymbolSummary(
        symbol_id=symbol.id,
        symbol=symbol.symbol,
        name=symbol.name,
        message_score=snapshot.message_score,
        sentiment=snapshot.sentiment,
        risk_level=snapshot.risk_level,
        confidence=snapshot.confidence,
        positive_count=snapshot.positive_count,
        negative_count=snapshot.negative_count,
        risk_count=snapshot.risk_count,
        latest_title=latest.title if latest is not None else snapshot.summary,
        events=[_event_read(event) for event in events],
    )


def _latest_macro_summary(db: Session, days: int) -> NewsMacroSummary | None:
    cutoff = datetime.utcnow() - timedelta(days=days)
    events = (
        db.execute(
            select(NewsEvent)
            .where(
                NewsEvent.symbol_id.is_(None),
                or_(NewsEvent.published_at.is_(None), NewsEvent.published_at >= cutoff),
            )
            .order_by(desc(NewsEvent.published_at), desc(NewsEvent.created_at))
            .limit(20)
        )
        .scalars()
        .all()
    )
    return _summarize_macro(events) if events else None


def get_latest_news(
    db: Session,
    *,
    portfolio_id: int | None = None,
    symbol_ids: list[int] | None = None,
    days: int = 7,
    limit: int = 20,
) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=days)
    ordered_ids: list[int] = []
    if symbol_ids:
        ordered_ids = list(dict.fromkeys(symbol_ids))[:limit]
    else:
        stmt = (
            select(NewsSnapshot)
            .where(NewsSnapshot.symbol_id.is_not(None), NewsSnapshot.created_at >= cutoff)
            .order_by(desc(NewsSnapshot.created_at), desc(NewsSnapshot.id))
            .limit(limit * 3)
        )
        if portfolio_id is not None:
            stmt = stmt.where(NewsSnapshot.portfolio_id == portfolio_id)
        seen: set[int] = set()
        for snapshot in db.execute(stmt).scalars().all():
            if snapshot.symbol_id is not None and snapshot.symbol_id not in seen:
                ordered_ids.append(snapshot.symbol_id)
                seen.add(snapshot.symbol_id)
            if len(ordered_ids) >= limit:
                break

    summaries: list[NewsSymbolSummary] = []
    for symbol_id in ordered_ids:
        stmt = (
            select(NewsSnapshot)
            .where(NewsSnapshot.symbol_id == symbol_id, NewsSnapshot.created_at >= cutoff)
            .order_by(desc(NewsSnapshot.created_at), desc(NewsSnapshot.id))
        )
        if portfolio_id is not None:
            stmt = stmt.where(NewsSnapshot.portfolio_id == portfolio_id)
        snapshot = db.execute(stmt).scalars().first()
        symbol = db.get(Symbol, symbol_id)
        if snapshot is not None and symbol is not None:
            summaries.append(_summary_from_snapshot(db, snapshot, symbol, days))

    return {
        "scope": "latest",
        "days": days,
        "symbols_total": len(summaries),
        "macro": _latest_macro_summary(db, days),
        "symbols": summaries,
        "failed": [],
    }


def update_news(db: Session, payload: NewsUpdateRequest) -> dict:
    symbols = _resolve_symbols(db, payload)
    failed: list[dict] = []
    macro_summary = None

    if payload.include_macro:
        macro_events = []
        for event in _fetch_macro_events(payload.days):
            try:
                macro_events.append(_upsert_event(db, event))
            except Exception as exc:
                failed.append({"scope": "macro", "error": str(exc)})
        macro_summary = _summarize_macro(macro_events)

    summaries = []
    for symbol in symbols:
        try:
            event_rows = _fetch_symbol_events(symbol, payload.days) if payload.include_symbol else []
            events = [_upsert_event(db, event) for event in event_rows]
            summaries.append(_summarize_symbol(db, payload, symbol, events))
        except Exception as exc:
            failed.append({"symbol_id": symbol.id, "symbol": symbol.symbol, "error": str(exc)})

    db.commit()
    return {
        "scope": payload.scope,
        "days": payload.days,
        "symbols_total": len(symbols),
        "macro": macro_summary,
        "symbols": summaries,
        "failed": failed,
    }
