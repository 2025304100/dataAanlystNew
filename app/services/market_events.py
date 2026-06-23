from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

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

# ── Keyword maps for auto-classification ──────────────────────────────

SCOPE_KEYWORDS: dict[str, list[str]] = {
    "macro_policy": [
        "央行", "降息", "降准", "利率", "LPR", "MLF", "逆回购", "存款准备金",
        "财政部", "财政", "国债", "地方债", "专项债", "减税", "税收",
        "证监会", "银保监会", "金融监管", "注册制", "退市制度",
        "国务院", "国常会", "政治局", "中央经济", "两会",
        "货币政策", "财政政策", "宏观调控", "稳增长", "保就业",
    ],
    "sector_dynamics": [
        "行业", "产业", "板块", "赛道", "半导体", "芯片", "新能源", "光伏",
        "锂电", "电池", "汽车", "医药", "医疗", "消费", "白酒", "地产",
        "房地产", "互联网", "人工智能", "AI", "数字经济", "数据",
        "技术突破", "国产替代", "供应链", "产能",
    ],
    "international": [
        "美联储", "美债", "美元", "美股", "美国", "特朗普", "拜登",
        "贸易战", "关税", "制裁", "地缘", "冲突", "战争", "中东",
        "欧盟", "欧洲", "日本", "日经", "亚太", "新兴市场",
        "IMF", "世界银行", "G7", "G20", "北约",
    ],
    "breaking": [
        "突发", "紧急", "地震", "洪水", "台风", "疫情", "病毒",
        "黑天鹅", "暴雷", "崩盘", "熔断", "暴跌", "暴涨",
        "战争", "恐袭", "坠机", "事故", "灾难",
    ],
    "fund_flow": [
        "北向资金", "南向资金", "外资", "主力资金", "资金流向",
        "成交额", "成交量", "流动性", "社融", "M2", "信贷",
        "融资融券", "两融", "ETF", "基金", "申购", "赎回",
    ],
    "sentiment": [
        "恐慌", "贪婪", "恐慌指数", "VIX", "情绪", "信心",
        "舆情", "舆论", "热搜", "关注度", "预期", "不确定性",
        "牛市", "熊市", "股灾",
    ],
}

LEVEL_KEYWORDS: dict[int, list[str]] = {
    5: [
        "突发", "紧急", "重大", "重磅", "降息", "降准", "贸易战", "战争",
        "制裁", "崩盘", "熔断", "黑天鹅", "暴雷", "股灾", "金融危机",
        "政治局", "国务院", "中央", "央行紧急",
    ],
    4: [
        "政策", "监管", "调控", "行业政策", "产业政策", "美联储",
        "关税", "通胀", "CPI", "PPI", "GDP", "PMI",
        "非农", "加息", "缩表", "退市", "注册制",
    ],
    3: [
        "部委", "部门", "规划", "意见", "方案", "通知",
        "数据", "指标", "报告", "指数", "市场", "走势",
        "北向", "主力", "资金", "板块", "轮动",
    ],
    2: [
        "行业", "产业", "企业", "公司", "公告", "财报",
        "季度", "年度", "发布会", "会议", "论坛",
    ],
}

SENTIMENT_KEYWORDS: dict[str, list[str]] = {
    "positive": [
        "利好", "上涨", "大涨", "反弹", "突破", "新高", "牛市",
        "增长", "提升", "改善", "积极", "乐观", "宽松", "刺激",
        "降息", "降准", "放水", "复苏", "回暖", "企稳",
    ],
    "negative": [
        "利空", "下跌", "暴跌", "大跌", "崩盘", "新低", "熊市",
        "下滑", "下降", "恶化", "消极", "悲观", "紧缩", "收紧",
        "加息", "上调", "危机", "衰退", "滞胀", "恐慌",
    ],
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _classify_impact_scope(title: str, summary: str | None) -> str:
    text = f"{title} {summary or ''}"
    for scope, keywords in SCOPE_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return scope
    return "other"


def _classify_importance_level(title: str, summary: str | None) -> int:
    text = f"{title} {summary or ''}"
    for level in range(5, 0, -1):
        for kw in LEVEL_KEYWORDS.get(level, []):
            if kw in text:
                return level
    return 2


def _classify_sentiment(title: str, summary: str | None) -> str:
    text = f"{title} {summary or ''}"
    pos = sum(1 for kw in SENTIMENT_KEYWORDS["positive"] if kw in text)
    neg = sum(1 for kw in SENTIMENT_KEYWORDS["negative"] if kw in text)
    if pos > neg:
        return "positive"
    elif neg > pos:
        return "negative"
    return "neutral"


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


# ── CRUD ──────────────────────────────────────────────────────────────


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

    # Count total
    count_stmt = stmt.with_only_columns(func.count(MarketEvent.id))
    total = db.execute(count_stmt).scalar() or 0

    # Sort
    sort_col = getattr(MarketEvent, sort_by, MarketEvent.published_at)
    stmt = stmt.order_by(desc(sort_col)).limit(limit).offset(offset)

    events = db.execute(stmt).scalars().all()

    # Aggregations
    scope_rows = (
        db.execute(
            select(MarketEvent.impact_scope, func.count(MarketEvent.id))
            .group_by(MarketEvent.impact_scope)
        )
        .all()
    )
    by_scope = {row[0]: row[1] for row in scope_rows}

    level_rows = (
        db.execute(
            select(MarketEvent.importance_level, func.count(MarketEvent.id))
            .group_by(MarketEvent.importance_level)
        )
        .all()
    )
    by_level = {str(row[0]): row[1] for row in level_rows}

    return MarketEventListResponse(
        events=[_event_to_read(e) for e in events],
        total=total,
        by_scope=by_scope,
        by_level=by_level,
    )


def get_market_event(db: Session, event_id: int) -> MarketEvent | None:
    return db.get(MarketEvent, event_id)


def create_market_event(db: Session, payload: MarketEventCreate) -> MarketEvent:
    event = MarketEvent(
        title=payload.title,
        summary=payload.summary,
        impact_scope=payload.impact_scope,
        importance_level=payload.importance_level,
        affected_market=payload.affected_market,
        affected_sectors=payload.affected_sectors,
        affected_symbols=payload.affected_symbols,
        sentiment=payload.sentiment,
        source="manual",
        source_url=payload.source_url,
        is_manual=1,
        published_at=payload.published_at or _now(),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def update_market_event(db: Session, event_id: int, payload: MarketEventUpdate) -> MarketEvent | None:
    event = db.get(MarketEvent, event_id)
    if event is None:
        return None
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
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


# ── Auto-collection ───────────────────────────────────────────────────


def _fetch_cctv_news() -> list[dict]:
    events: list[dict] = []
    try:
        with quiet_akshare_output():
            frame = ak.news_cctv()
        for row in frame.head(30).to_dict("records"):
            title = _first_value(row, ["新闻标题", "标题", "title", "内容", "摘要"])
            if not title:
                continue
            published_at = _parse_datetime(_first_value(row, ["发布时间", "时间", "date", "日期"]))
            events.append({
                "title": str(title),
                "source": "cctv",
                "published_at": published_at,
            })
    except Exception:
        logger.debug("Failed to fetch CCTV news", exc_info=True)
    return events


def _fetch_baidu_economic_news() -> list[dict]:
    events: list[dict] = []
    try:
        with quiet_akshare_output():
            frame = ak.news_economic_baidu()
        for row in frame.head(30).to_dict("records"):
            title = _first_value(row, ["新闻标题", "标题", "title", "内容", "摘要"])
            if not title:
                continue
            published_at = _parse_datetime(_first_value(row, ["发布时间", "时间", "date", "日期"]))
            url = _first_value(row, ["新闻链接", "链接", "url"])
            events.append({
                "title": str(title),
                "source": "baidu",
                "source_url": str(url) if url else None,
                "published_at": published_at,
            })
    except Exception:
        logger.debug("Failed to fetch Baidu economic news", exc_info=True)
    return events


def _fetch_baidu_report_news() -> list[dict]:
    events: list[dict] = []
    try:
        with quiet_akshare_output():
            frame = ak.news_report_time_baidu()
        for row in frame.head(30).to_dict("records"):
            title = _first_value(row, ["新闻标题", "标题", "title", "内容", "摘要"])
            if not title:
                continue
            published_at = _parse_datetime(_first_value(row, ["发布时间", "时间", "date", "日期"]))
            url = _first_value(row, ["新闻链接", "链接", "url"])
            events.append({
                "title": str(title),
                "source": "baidu-report",
                "source_url": str(url) if url else None,
                "published_at": published_at,
            })
    except Exception:
        logger.debug("Failed to fetch Baidu report news", exc_info=True)
    return events


def collect_market_events(db: Session, payload: MarketEventCollectRequest) -> MarketEventCollectResponse:
    source_map = {
        "cctv": _fetch_cctv_news,
        "baidu": _fetch_baidu_economic_news,
        "baidu-report": _fetch_baidu_report_news,
    }

    all_raw: list[dict] = []
    for src in payload.sources:
        fetcher = source_map.get(src)
        if fetcher is None:
            continue
        try:
            all_raw.extend(fetcher())
        except Exception:
            logger.debug("Failed to fetch from source %s", src, exc_info=True)

    cutoff = _now() - timedelta(days=payload.days)
    seen_hashes: set[str] = set()

    # Preload existing title hashes for dedup
    existing = db.execute(select(MarketEvent.title)).scalars().all()
    seen_hashes = {_title_hash(t) for t in existing}

    collected = 0
    skipped = 0
    errors: list[str] = []

    for raw in all_raw:
        title = raw["title"]
        h = _title_hash(title)
        if h in seen_hashes:
            skipped += 1
            continue
        seen_hashes.add(h)

        published_at = raw.get("published_at")
        if published_at and published_at < cutoff:
            skipped += 1
            continue

        try:
            impact_scope = _classify_impact_scope(title, None)
            importance_level = _classify_importance_level(title, None)
            sentiment = _classify_sentiment(title, None)

            event = MarketEvent(
                title=title,
                impact_scope=impact_scope,
                importance_level=importance_level,
                affected_market="A股",
                sentiment=sentiment,
                source=raw["source"],
                source_url=raw.get("source_url"),
                is_manual=0,
                published_at=published_at or _now(),
            )
            db.add(event)
            collected += 1
        except Exception as exc:
            errors.append(f"Failed to save '{title[:50]}': {exc}")
            logger.warning("Failed to save market event: %s", exc)

    db.commit()
    logger.info("Market event collection: %d collected, %d skipped", collected, skipped)

    return MarketEventCollectResponse(
        collected=collected,
        skipped_duplicate=skipped,
        errors=errors,
    )