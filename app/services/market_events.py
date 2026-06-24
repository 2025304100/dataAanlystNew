from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import akshare as ak
import pandas as pd
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.market_event import MarketEvent
from app.schemas.market_event import (
    MarketEventCollectRequest,
    MarketEventCollectResponse,
    MarketEventCreate,
    MarketEventListResponse,
    MarketEventRead,
    MarketEventUpdate,
)
from app.services.akshare_utils import quiet_akshare_output

logger = logging.getLogger(__name__)

SCOPE_KEYWORDS: dict[str, list[str]] = {
    "macro_policy": ["??", "??", "??", "??", "LPR", "MLF", "??", "??", "??", "??", "???", "???", "??", "CPI", "PPI", "PMI"],
    "commodity_futures": ["??", "??", "??", "??", "?", "?", "?", "?", "??", "??", "??", "??", "??", "??", "??", "??", "???", "??", "OPEC", "COMEX", "LME"],
    "sector_dynamics": ["??", "??", "??", "???", "??", "???", "??", "??", "??", "??", "??", "AI", "????", "??", "??"],
    "international": ["???", "??", "??", "??", "??", "??", "??", "??", "??", "??", "??", "??", "??", "IMF", "G7", "G20"],
    "breaking": ["??", "??", "??", "??", "??", "??", "???", "??", "??", "??", "??", "??", "??"],
    "fund_flow": ["????", "????", "??", "????", "???", "???", "???", "???", "??", "M2", "??", "????", "ETF", "??"],
    "sentiment": ["??", "??", "VIX", "??", "??", "??", "??", "???", "??", "??"],
}

LEVEL_KEYWORDS: dict[int, list[str]] = {
    5: ["??", "??", "??", "??", "??", "??", "??", "??", "??", "??", "???", "??", "????"],
    4: ["??", "??", "??", "???", "??", "??", "CPI", "PPI", "GDP", "PMI", "??", "??", "??", "OPEC"],
    3: ["??", "??", "??", "??", "??", "??", "??", "??", "??", "??", "??", "??"],
    2: ["??", "??", "??", "??", "??", "??", "??"],
}

SENTIMENT_KEYWORDS: dict[str, list[str]] = {
    "positive": [
        "利好","上涨","反弹","回升","走强","突破","创新高","大涨","攀升",
        "提振","推动","支撑","看好","乐观","增持","买入","资金流入",
        "超预期","向好","复苏","繁荣","景气","盈利","增长","利好政策",
        "降息","宽松","刺激","救助","扶持","补贴","减税","改革","开放",
    ],
    "negative": [
        "利空","下跌","暴跌","回落","走弱","破位","创新低","大跌","暴跌",
        "压制","打击","拖累","看空","悲观","减持","卖出","资金流出",
        "低于预期","恶化","衰退","萧条","亏损","下滑","收紧","加息",
        "制裁","封锁","违约","暴雷","退市","停牌","调查","处罚","风险",
    ],
}

SOURCE_CREDIBILITY = {"cctv": 5, "baidu-report": 4, "baidu": 3, "eastmoney-global": 4, "caixin": 4, "futures-shmet": 3, "manual": 4}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().replace(tzinfo=None)


def _text_values(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in row.values():
        if value is None or pd.isna(value):
            continue
        text = str(value).strip()
        if text and text.lower() != "nan":
            values.append(text)
    return values


def _looks_like_url(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _pick_title(row: dict[str, Any]) -> str | None:
    preferred_keys = ["title", "????", "??", "??", "??", "articleTitle", "infoTitle"]
    for key in preferred_keys:
        value = row.get(key)
        if value is not None and not pd.isna(value):
            text = str(value).strip()
            if len(text) >= 6 and not _looks_like_url(text):
                return text
    for text in _text_values(row):
        if 8 <= len(text) <= 180 and not _looks_like_url(text) and not _parse_datetime(text):
            return text
    return None


def _pick_summary(row: dict[str, Any], title: str) -> str | None:
    preferred_keys = ["summary", "??", "??", "description", "digest"]
    for key in preferred_keys:
        value = row.get(key)
        if value is None or pd.isna(value):
            continue
        text = str(value).strip()
        if text and text != title and not _looks_like_url(text):
            return text[:500]
    return None


def _pick_url(row: dict[str, Any]) -> str | None:
    for key in ["url", "??", "????", "source_url", "articleUrl"]:
        value = row.get(key)
        if value is not None and not pd.isna(value):
            text = str(value).strip()
            if _looks_like_url(text):
                return text
    for text in _text_values(row):
        if _looks_like_url(text):
            return text
    return None


def _pick_published_at(row: dict[str, Any]) -> datetime | None:
    for key in ["date", "??", "??", "????", "publish_time", "showTime", "time"]:
        if key in row:
            parsed = _parse_datetime(row.get(key))
            if parsed is not None:
                return parsed
    for value in row.values():
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _classify_impact_scope(title: str, summary: str | None) -> str:
    text = f"{title} {summary or ''}"
    for scope, keywords in SCOPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return scope
    return "other"


def _classify_importance_level(title: str, summary: str | None) -> int:
    text = f"{title} {summary or ''}"
    for level in range(5, 1, -1):
        if any(keyword in text for keyword in LEVEL_KEYWORDS.get(level, [])):
            return level
    return 2


def _classify_sentiment(title: str, summary: str | None) -> str:
    text = f"{title} {summary or ''}"
    pos = sum(1 for keyword in SENTIMENT_KEYWORDS["positive"] if keyword in text)
    neg = sum(1 for keyword in SENTIMENT_KEYWORDS["negative"] if keyword in text)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _affected_market(scope: str) -> str:
    if scope == "commodity_futures":
        return "??/??"
    if scope == "international":
        return "??/A?"
    return "A?"


def _title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def _event_to_read(event: MarketEvent) -> MarketEventRead:
    return MarketEventRead(
        id=event.id,
        title=event.title,
        summary=event.summary,
        impact_scope=event.impact_scope,
        importance_level=event.importance_level,
        affected_market=event.affected_market,
        affected_sectors=event.affected_sectors,
        affected_symbols=event.affected_symbols,
        sentiment=event.sentiment,
        source=event.source,
        source_url=event.source_url,
        is_manual=event.is_manual,
        published_at=event.published_at,
        expires_at=event.expires_at,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def _parse_date_str(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def list_market_events(
    db: Session,
    *,
    impact_scope: str | None = None,
    importance_level_min: int | None = None,
    importance_level_max: int | None = None,
    affected_market: str | None = None,
    sentiment: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    is_manual: int | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "published_at",
) -> MarketEventListResponse:
    stmt = select(MarketEvent)
    if impact_scope:
        stmt = stmt.where(MarketEvent.impact_scope == impact_scope)
    if importance_level_min is not None:
        stmt = stmt.where(MarketEvent.importance_level >= importance_level_min)
    if importance_level_max is not None:
        stmt = stmt.where(MarketEvent.importance_level <= importance_level_max)
    if affected_market:
        stmt = stmt.where(MarketEvent.affected_market == affected_market)
    if sentiment:
        stmt = stmt.where(MarketEvent.sentiment == sentiment)
    if date_from:
        parsed_from = _parse_date_str(date_from)
        if parsed_from is not None:
            stmt = stmt.where(MarketEvent.published_at >= parsed_from)
    if date_to:
        parsed_to = _parse_date_str(date_to)
        if parsed_to is not None:
            stmt = stmt.where(MarketEvent.published_at <= parsed_to)
    if is_manual is not None:
        stmt = stmt.where(MarketEvent.is_manual == is_manual)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    if sort_by == "importance_level":
        stmt = stmt.order_by(desc(MarketEvent.importance_level), desc(MarketEvent.published_at), desc(MarketEvent.created_at))
    else:
        stmt = stmt.order_by(desc(MarketEvent.published_at), desc(MarketEvent.created_at))
    events = db.execute(stmt.offset(offset).limit(limit)).scalars().all()

    all_events = db.execute(select(MarketEvent)).scalars().all()
    by_scope: dict[str, int] = {}
    by_level: dict[str, int] = {}
    for event in all_events:
        by_scope[event.impact_scope] = by_scope.get(event.impact_scope, 0) + 1
        level_key = str(event.importance_level)
        by_level[level_key] = by_level.get(level_key, 0) + 1

    return MarketEventListResponse(events=[_event_to_read(e) for e in events], total=total, by_scope=by_scope, by_level=by_level)


def get_market_event(db: Session, event_id: int) -> MarketEvent | None:
    return db.get(MarketEvent, event_id)


def create_market_event(db: Session, payload: MarketEventCreate) -> MarketEvent:
    event = MarketEvent(**payload.model_dump(exclude_unset=True), source="manual", is_manual=1)
    if event.published_at is None:
        event.published_at = _now()
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def update_market_event(db: Session, event_id: int, payload: MarketEventUpdate) -> MarketEvent | None:
    event = db.get(MarketEvent, event_id)
    if event is None:
        return None
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(event, key, value)
    db.commit()
    db.refresh(event)
    return event


def delete_market_event(db: Session, event_id: int) -> bool:
    event = db.get(MarketEvent, event_id)
    if event is None:
        return False
    db.delete(event)
    db.commit()
    return True


def _fetch_from_ak(fetcher: Callable[[], Any], source: str, limit: int = 40) -> list[dict]:
    events: list[dict] = []
    try:
        with quiet_akshare_output():
            frame = fetcher()
        if frame is None or getattr(frame, "empty", True):
            return events
        for row in frame.head(limit).to_dict("records"):
            title = _pick_title(row)
            if not title:
                continue
            events.append({
                "title": title,
                "summary": _pick_summary(row, title),
                "source": source,
                "source_url": _pick_url(row),
                "published_at": _pick_published_at(row),
            })
    except Exception as exc:
        logger.debug("Failed to fetch %s: %s", source, exc, exc_info=True)
    return events


def _fetch_cctv_news() -> list[dict]:
    return _fetch_from_ak(ak.news_cctv, "cctv", 40)


def _fetch_baidu_economic_news() -> list[dict]:
    return _fetch_from_ak(ak.news_economic_baidu, "baidu", 40)


def _fetch_baidu_report_news() -> list[dict]:
    return _fetch_from_ak(ak.news_report_time_baidu, "baidu-report", 40)


def _fetch_global_news() -> list[dict]:
    return _fetch_from_ak(ak.stock_info_global_em, "eastmoney-global", 50)


def _fetch_caixin_news() -> list[dict]:
    return _fetch_from_ak(ak.stock_news_main_cx, "caixin", 40)


def _fetch_futures_news() -> list[dict]:
    return _fetch_from_ak(ak.futures_news_shmet, "futures-shmet", 30)


def collect_market_events(db: Session, payload: MarketEventCollectRequest) -> MarketEventCollectResponse:
    source_map: dict[str, Callable[[], list[dict]]] = {
        "eastmoney-global": _fetch_global_news,
        "caixin": _fetch_caixin_news,
        "cctv": _fetch_cctv_news,
        "baidu": _fetch_baidu_economic_news,
        "baidu-report": _fetch_baidu_report_news,
        "futures-shmet": _fetch_futures_news,
    }

    all_raw: list[dict] = []
    errors: list[str] = []
    for source in payload.sources:
        fetcher = source_map.get(source)
        if fetcher is None:
            errors.append(f"Unknown source: {source}")
            continue
        try:
            all_raw.extend(fetcher())
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            logger.debug("Failed to fetch source %s", source, exc_info=True)

    cutoff = _now() - timedelta(days=payload.days)
    existing = db.execute(select(MarketEvent.title)).scalars().all()
    seen_hashes = {_title_hash(title) for title in existing}
    collected = 0
    skipped = 0

    for raw in all_raw:
        title = str(raw.get("title") or "").strip()
        if not title:
            skipped += 1
            continue
        title_hash = _title_hash(title)
        if title_hash in seen_hashes:
            skipped += 1
            continue
        seen_hashes.add(title_hash)

        published_at = raw.get("published_at") or _now()
        if published_at and published_at < cutoff:
            skipped += 1
            continue

        summary = raw.get("summary")
        impact_scope = _classify_impact_scope(title, summary)
        importance_level = _classify_importance_level(title, summary)
        sentiment = _classify_sentiment(title, summary)
        expires_days = 14 if importance_level >= 4 else 7 if importance_level >= 3 else 3

        db.add(MarketEvent(
            title=title,
            summary=summary,
            impact_scope=impact_scope,
            importance_level=importance_level,
            affected_market=_affected_market(impact_scope),
            sentiment=sentiment,
            source=raw.get("source") or "unknown",
            source_url=raw.get("source_url"),
            is_manual=0,
            published_at=published_at,
            expires_at=published_at + timedelta(days=expires_days) if published_at else None,
        ))
        collected += 1

    db.commit()
    return MarketEventCollectResponse(collected=collected, skipped_duplicate=skipped, errors=errors)
