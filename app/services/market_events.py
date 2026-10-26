from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import akshare as ak
import pandas as pd
from pathlib import Path
from sqlalchemy import case, delete, desc, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.market_event_policy import MARKET_EVENT_KEEP_DAYS_DEFAULT, effective_keep_days
from app.services.market_event_collection_lock import market_event_write_lock

from app.models.investment_theme import ThemeCatalyst
from app.models.market_event import MarketEvent, MarketEventCollectRun
from app.schemas.market_event import (
    MarketEventCollectRequest,
    MarketEventCollectResponse,
    MarketEventCreate,
    MarketEventListResponse,
    MarketEventRead,
    MarketEventUpdate,
)
from app.services.akshare_utils import quiet_akshare_output
from app.services.sector_concepts import (
    KEYWORD_TO_SECTOR, build_index, event_sector_ids, map_sectors_on,
    map_to_sectors, map_to_sectors_db, parse_affected_sectors, resolve_concepts,
)

logger = logging.getLogger(__name__)

# P0-2：全部已实现采集源（前端按钮与定时任务的默认全集）
ALL_COLLECT_SOURCES = [
    "eastmoney-global", "caixin", "cctv", "baidu", "baidu-report", "futures-shmet",
]

# 保留最长一年热点窗口；旧 90 天排程在服务层安全提升到 365 天。
CLEANUP_KEEP_DAYS_DEFAULT = MARKET_EVENT_KEEP_DAYS_DEFAULT

SCOPE_KEYWORDS: dict[str, list[str]] = {
    "macro_policy": ["政策", "央行", "降准", "降息", "加息", "利率", "LPR", "MLF", "财政", "货币", "CPI", "PPI", "PMI", "GDP", "社融", "M2", "国债", "美债", "通胀", "就业", "非农", "美联储", "国务院", "发改委", "证监会", "政治局"],
    "commodity_futures": ["原油", "黄金", "白银", "铜", "铝", "锌", "铁矿", "焦煤", "焦炭", "螺纹", "煤炭", "天然气", "农产品", "大豆", "玉米", "棉花", "期货", "大宗商品", "OPEC", "COMEX", "LME", "SHFE", "DCE", "CZCE"],
    "sector_dynamics": ["行业", "板块", "新能源", "半导体", "芯片", "人工智能", "AI", "算力", "机器人", "医药", "地产", "消费", "军工", "银行", "券商", "保险", "汽车", "光伏", "锂电"],
    "international": ["美国", "欧洲", "日本", "韩国", "印度", "俄乌", "中东", "美元", "美股", "港股", "纳斯达克", "道琼斯", "标普", "美联储", "IMF", "G7", "G20", "关税", "贸易"],
    "breaking": ["突发", "地震", "战争", "冲突", "制裁", "事故", "停产", "爆炸", "调查", "处罚", "违约", "退市", "停牌", "监管", "黑天鹅"],
    "fund_flow": ["北向资金", "南向资金", "主力资金", "融资融券", "融资余额", "ETF", "流入", "流出", "增持", "减持", "回购", "募资", "社融", "M2", "成交额", "放量", "缩量"],
    "sentiment": ["情绪", "恐慌", "风险偏好", "VIX", "避险", "乐观", "悲观", "信心", "预期", "热度", "活跃"],
}

LEVEL_KEYWORDS: dict[int, list[str]] = {
    5: ["突发", "战争", "冲突", "制裁", "系统性风险", "金融危机", "崩盘", "暴跌", "熔断", "违约", "停牌", "退市", "重大事故", "紧急", "黑天鹅"],
    4: ["央行", "美联储", "降准", "降息", "加息", "CPI", "PPI", "GDP", "PMI", "LPR", "MLF", "社融", "M2", "国债", "美债", "OPEC", "监管", "政策", "政治局"],
    3: ["行业", "板块", "资金", "融资", "北向", "南向", "财报", "业绩", "并购", "回购", "订单", "出口", "库存", "涨价", "降价"],
    2: ["发布", "公告", "数据", "报告", "会议", "观点", "提示"],
}

SENTIMENT_KEYWORDS: dict[str, list[str]] = {
    "positive": ["利好", "上涨", "反弹", "回升", "走强", "突破", "创新高", "大涨", "提振", "推动", "支撑", "看好", "乐观", "增持", "买入", "资金流入", "超预期", "向好", "复苏", "盈利", "增长", "宽松", "刺激", "扶持", "补贴", "减税", "改革", "开放"],
    "negative": ["利空", "下跌", "暴跌", "回落", "走弱", "破位", "创新低", "大跌", "压制", "打击", "拖累", "看空", "悲观", "减持", "卖出", "资金流出", "低于预期", "恶化", "衰退", "亏损", "下滑", "收紧", "加息", "制裁", "封锁", "违约", "暴雷", "退市", "停牌", "调查", "处罚", "风险"],
}

SOURCE_CREDIBILITY = {"cctv": 5, "baidu-report": 4, "baidu": 3, "eastmoney-global": 4, "caixin": 4, "futures-shmet": 3, "manual": 4}

# ── P1-5：词表外置配置 ──────────────────────────────────────
# data/config/market_event_keywords.json 可覆盖默认词表（同名键整体替换），
# 调词不用改代码发版；文件缺失/损坏时静默回退内置默认。按 mtime 缓存。
_KEYWORD_OVERRIDE_FILE = Path(__file__).resolve().parents[2] / "data" / "config" / "market_event_keywords.json"
_keyword_cache: dict = {"mtime": None, "scope": None, "level": None, "sentiment": None}


def _merged_keywords() -> tuple[dict, dict, dict]:
    import json

    try:
        mtime = _KEYWORD_OVERRIDE_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None and _keyword_cache["mtime"] == mtime:
        return _keyword_cache["scope"], _keyword_cache["level"], _keyword_cache["sentiment"]

    scope = {k: list(v) for k, v in SCOPE_KEYWORDS.items()}
    level = {k: list(v) for k, v in LEVEL_KEYWORDS.items()}
    sentiment = {k: list(v) for k, v in SENTIMENT_KEYWORDS.items()}
    if mtime is not None:
        try:
            with open(_KEYWORD_OVERRIDE_FILE, encoding="utf-8") as fh:
                overrides = json.load(fh)
            if not isinstance(overrides, dict):
                raise ValueError("keyword config must be an object")
            for key, target in (("impact_scope", scope), ("importance_level", level), ("sentiment", sentiment)):
                section = overrides.get(key, {})
                if not isinstance(section, dict):
                    raise ValueError(f"{key} must be an object")
                for name, words in section.items():
                    if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
                        raise ValueError(f"{key}.{name} must be a list of non-empty strings")
                    normalized_key = int(name) if key == "importance_level" else str(name)
                    if key == "importance_level" and normalized_key not in LEVEL_KEYWORDS:
                        raise ValueError("importance_level must be between 2 and 5")
                    if key == "sentiment" and normalized_key not in SENTIMENT_KEYWORDS:
                        raise ValueError("unknown sentiment")
                    target[normalized_key] = [w.strip() for w in words]
        except (OSError, ValueError, TypeError):
            logger.warning("market_event_keywords.json 解析失败，回退内置默认词表", exc_info=True)
            scope = {k: list(v) for k, v in SCOPE_KEYWORDS.items()}
            level = {k: list(v) for k, v in LEVEL_KEYWORDS.items()}
            sentiment = {k: list(v) for k, v in SENTIMENT_KEYWORDS.items()}
            # Cache the fallback for this file version; retry after mtime changes.
    _keyword_cache.update({"mtime": mtime, "scope": scope, "level": level, "sentiment": sentiment})
    return scope, level, sentiment

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Any, *, assume_timezone=None) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    parsed = parsed.to_pydatetime()
    if parsed.tzinfo is None and assume_timezone is not None:
        parsed = parsed.replace(tzinfo=assume_timezone)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


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
    preferred_keys = ["title", "标题", "新闻标题", "文章标题", "资讯标题", "articleTitle", "infoTitle"]
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
    preferred_keys = ["summary", "摘要", "内容", "正文", "description", "digest"]
    for key in preferred_keys:
        value = row.get(key)
        if value is None or pd.isna(value):
            continue
        text = str(value).strip()
        if text and text != title and not _looks_like_url(text):
            return text[:500]
    return None


def _pick_url(row: dict[str, Any]) -> str | None:
    for key in ["url", "链接", "网址", "新闻链接", "source_url", "articleUrl"]:
        value = row.get(key)
        if value is not None and not pd.isna(value):
            text = str(value).strip()
            if _looks_like_url(text):
                return text
    for text in _text_values(row):
        if _looks_like_url(text):
            return text
    return None


def _pick_published_at(row: dict[str, Any], *, assume_timezone=None) -> datetime | None:
    for key in ["date", "日期", "时间", "发布时间", "publish_time", "showTime", "time"]:
        if key in row:
            parsed = _parse_datetime(row.get(key), assume_timezone=assume_timezone)
            if parsed is not None:
                return parsed
    for value in row.values():
        parsed = _parse_datetime(value, assume_timezone=assume_timezone)
        if parsed is not None:
            return parsed
    return None


def _classify_impact_scope(title: str, summary: str | None) -> str:
    """P1-5：按命中关键词数加权取最高（消除词表顺序敏感）。

    旧行为是"字典序首个命中即返回"——"美联储"同时属于宏观与国际，谁在
    前算谁。改为计数后，多命中证据的分类胜出；平局保持原字典序优先；
    全部未命中仍归 other。
    """
    return _analyze_classification(title,summary)["current_scope"]


def _classify_importance_level(title: str, summary: str | None) -> int:
    return _analyze_classification(title,summary)["current_level"]


def _classify_sentiment(title: str, summary: str | None) -> str:
    return _analyze_classification(title,summary)["current_sentiment"]


def _analyze_classification(title,summary):
    from app.services.market_event_classification import analyze_market_text
    return analyze_market_text(title,summary,*_merged_keywords())


def _current_classification_analysis(event):
    analysis=_analyze_classification(event.title,event.summary)
    analysis["matches_saved"]=(analysis["current_scope"],analysis["current_level"],analysis["current_sentiment"]) == (
        event.impact_scope,event.importance_level,event.sentiment)
    return analysis


def _affected_market(scope: str) -> str:
    if scope == "commodity_futures":
        return "期货/商品"
    if scope == "international":
        return "美股/A股"
    return "A股/期货"


def _title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def _event_to_read(event: MarketEvent, sector_index: dict[str, str] | None = None) -> MarketEventRead:
    return MarketEventRead(
        id=event.id,
        title=event.title,
        summary=event.summary,
        impact_scope=event.impact_scope,
        importance_level=event.importance_level,
        affected_market=event.affected_market,
        affected_sectors=",".join(event_sector_ids(event.affected_sectors, event.title, event.summary,
                                                  KEYWORD_TO_SECTOR if sector_index is None else sector_index)),
        sector_tag_source=event.sector_tag_source or ("legacy" if event.affected_sectors is not None else "auto"),
        affected_symbols=event.affected_symbols,
        sentiment=event.sentiment,
        classification_analysis=_current_classification_analysis(event),
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


def market_event_to_read(db: Session, event: MarketEvent) -> MarketEventRead:
    index = build_index(resolve_concepts(db)) if event.affected_sectors is None else {}
    return _event_to_read(event, index)


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
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "published_at",
    include_expired: bool = True,
    event_ids: list[int] | None = None,
    as_of: datetime | None = None,
    snapshot_max_id: int | None = None,
) -> MarketEventListResponse:
    now = as_of or _now()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    max_id = snapshot_max_id if snapshot_max_id is not None else int(db.scalar(select(func.max(MarketEvent.id))) or 0)
    stmt = select(MarketEvent).where(MarketEvent.id <= max_id)
    if event_ids is not None:
        stmt = stmt.where(MarketEvent.id.in_(event_ids))
    if keyword:
        # P1-6：关键词检索（标题/摘要包含；contains 自带 autoescape 处理 %/_ 通配符）
        needle = keyword.strip()
        stmt = stmt.where(or_(MarketEvent.title.contains(needle, autoescape=True),
                              MarketEvent.summary.contains(needle, autoescape=True)))
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

    expired_total = db.scalar(select(func.count()).select_from(
        stmt.where(MarketEvent.expires_at < now).subquery()
    )) or 0
    if not include_expired:
        stmt = stmt.where(or_(MarketEvent.expires_at.is_(None), MarketEvent.expires_at >= now))

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    if sort_by == "importance_level":
        # P1-5：同级并列时按来源权威性兜底排序（SOURCE_CREDIBILITY 首次真正参与排序），
        # 权威源在前、未知来源按最低档 2 处理；时间仍是第三序。
        credibility = case(
            *[(MarketEvent.source == src, score) for src, score in SOURCE_CREDIBILITY.items()],
            else_=2,
        )
        stmt = stmt.order_by(
            desc(MarketEvent.importance_level),
            desc(credibility),
            desc(MarketEvent.published_at),
            desc(MarketEvent.created_at),
            desc(MarketEvent.id),
        )
    else:
        stmt = stmt.order_by(desc(MarketEvent.published_at), desc(MarketEvent.created_at), desc(MarketEvent.id))
    events = db.execute(stmt.offset(offset).limit(limit)).scalars().all()

    # Keep the existing global scope-card contract without loading every ORM row.
    stats_conditions = [MarketEvent.id <= max_id]
    if not include_expired:
        stats_conditions.append(or_(MarketEvent.expires_at.is_(None), MarketEvent.expires_at >= now))
    by_scope = dict(db.execute(select(MarketEvent.impact_scope, func.count()).where(*stats_conditions)
                               .group_by(MarketEvent.impact_scope)).all())
    by_level = {str(level): count for level, count in db.execute(
        select(MarketEvent.importance_level, func.count()).where(*stats_conditions).group_by(MarketEvent.importance_level)
    ).all()}
    index = build_index(resolve_concepts(db)) if any(e.affected_sectors is None for e in events) else {}
    return MarketEventListResponse(events=[_event_to_read(e, index) for e in events], total=total,
                                   by_scope=by_scope, by_level=by_level, expired_total=expired_total,
                                   as_of=now, snapshot_max_id=max_id)


def get_market_event(db: Session, event_id: int) -> MarketEvent | None:
    return db.get(MarketEvent, event_id)


def create_market_event(db: Session, payload: MarketEventCreate) -> MarketEvent:
    event = MarketEvent(**payload.model_dump(exclude_unset=True), source="manual", is_manual=1)
    if event.published_at is None:
        event.published_at = _now()
    # P0-4/P0-3：手工事件同样维护去重 hash；未显式给板块时自动打标
    event.title_hash = _title_hash(event.title)
    if event.affected_sectors is None:
        sectors = map_to_sectors_db(db, event.title, event.summary)
        event.affected_sectors = ",".join(sectors)
        event.sector_tag_source = "auto"
    else:
        event.affected_sectors = ",".join(parse_affected_sectors(event.affected_sectors))
        event.sector_tag_source = "manual"
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def update_market_event(db: Session, event_id: int, payload: MarketEventUpdate) -> MarketEvent | None:
    event = db.get(MarketEvent, event_id)
    if event is None:
        return None
    changes = payload.model_dump(exclude_unset=True)
    requested_source = changes.pop("sector_tag_source", None)
    old_source = event.sector_tag_source or ("legacy" if event.affected_sectors is not None else "auto")
    for key, value in changes.items():
        setattr(event, key, value)
    # 标题变更时同步去重 hash（保持与采集入库一致的不变量）
    if "title" in changes:
        event.title_hash = _title_hash(event.title or "")
    if requested_source == "auto" or ("affected_sectors" in changes and changes["affected_sectors"] is None):
        event.affected_sectors = ",".join(map_to_sectors_db(db, event.title, event.summary))
        event.sector_tag_source = "auto"
    elif "affected_sectors" in changes or requested_source == "manual":
        event.affected_sectors = ",".join(parse_affected_sectors(event.affected_sectors))
        event.sector_tag_source = "manual"
    elif {"title", "summary"} & changes.keys() and old_source == "auto":
        event.affected_sectors = ",".join(map_to_sectors_db(db, event.title, event.summary))
        event.sector_tag_source = "auto"
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


def _fetch_from_ak(fetcher: Callable[..., Any], source: str, limit: int = 40, **fetch_kwargs) -> list[dict]:
    events: list[dict] = []
    # 风控加固：用 call_akshare_with_retry 包装，给瞬时风控一次重试机会
    from app.services.akshare_utils import call_akshare_with_retry
    try:
        with quiet_akshare_output():
            frame = call_akshare_with_retry(fetcher, api_key=source, max_attempts=2, **fetch_kwargs)
        if frame is None or getattr(frame, "empty", True):
            return events
        for row in frame.head(limit).to_dict("records"):
            title = _pick_title(row)
            if not title:
                continue
            # These domestic feeds expose naive China local timestamps, whereas
            # stored/API timestamps use naive UTC. Explicit offsets are already
            # normalized by _parse_datetime and must not be shifted twice.
            published_at = _pick_published_at(row, assume_timezone=timezone(timedelta(hours=8)))
            events.append({
                "title": title,
                "summary": _pick_summary(row, title),
                "source": source,
                "source_url": _pick_url(row),
                "published_at": published_at,
            })
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", source, exc)
        raise
    return events


def _fetch_cctv_news() -> list[dict]:
    # Yesterday's complete broadcast is available even before today's evening
    # programme. Never rely on the dependency's hard-coded historical date.
    return _fetch_from_ak(ak.news_cctv, "cctv", 40, date=_supplier_date(days_back=1))


def _supplier_date(*, days_back: int = 0) -> str:
    return (_now() + timedelta(hours=8, days=-days_back)).strftime("%Y%m%d")


def _fetch_baidu_economic_news() -> list[dict]:
    from app.services.baidu_market_calendar import fetch_baidu_calendar
    return _fetch_from_ak(fetch_baidu_calendar, "baidu", 40, date=_supplier_date(), category="economic_data")


def _fetch_baidu_report_news() -> list[dict]:
    from app.services.baidu_market_calendar import fetch_baidu_calendar
    return _fetch_from_ak(fetch_baidu_calendar, "baidu-report", 40, date=_supplier_date(), category="report_time")


def _fetch_global_news() -> list[dict]:
    return _fetch_from_ak(ak.stock_info_global_em, "eastmoney-global", 50)


def _fetch_caixin_news() -> list[dict]:
    return _fetch_from_ak(ak.stock_news_main_cx, "caixin", 40)


def _fetch_futures_news() -> list[dict]:
    return _fetch_from_ak(ak.futures_news_shmet, "futures-shmet", 30)


def _backfill_title_hashes(db: Session) -> int:
    """P0-4：为历史缺 title_hash 的行回填（自愈，幂等）。

    迁移 0086 会回填存量；这里兜底两类环境：只跑过 create_all 的库、
    以及迁移后人工导入了无 hash 数据的库。行数不多时一次补齐，
    避免去重查询漏掉旧标题造成重复入库。
    """
    rows = db.execute(
        select(MarketEvent.id, MarketEvent.title).where(MarketEvent.title_hash.is_(None))
    ).all()
    for event_id, title in rows:
        db.execute(
            update(MarketEvent).where(MarketEvent.id == event_id).values(title_hash=_title_hash(title))
        )
    return len(rows)


def _existing_title_hashes(db: Session, candidate_hashes: set[str]) -> set[str]:
    """按候选 hash 分块查库，返回库中已存在的 hash 集合（走 title_hash 索引）。"""
    found: set[str] = set()
    ordered = list(candidate_hashes)
    chunk_size = 500
    for start in range(0, len(ordered), chunk_size):
        chunk = ordered[start:start + chunk_size]
        found.update(
            db.execute(
                select(MarketEvent.title_hash).where(MarketEvent.title_hash.in_(chunk))
            ).scalars().all()
        )
    return found


# P1-7：采集运行账目保留窗口（天）
COLLECT_RUNS_KEEP_DAYS = 90


def _record_collect_run(db: Session, *, trigger_source: str, payload: MarketEventCollectRequest,
                        collected: int, skipped: int, status: str, errors: list[str],
                        started_at) -> None:
    import json as _json

    try:
        from app.models.market_event import MarketEventCollectRun

        db.add(MarketEventCollectRun(
            trigger_source=trigger_source,
            days=payload.days,
            sources_json=_json.dumps(list(payload.sources), ensure_ascii=False),
            collected=collected,
            skipped_duplicate=skipped,
            status=status,
            errors_json=_json.dumps(errors[:20], ensure_ascii=False),
            started_at=started_at,
            finished_at=_now(),
        ))
        # 顺带清理窗口外的旧账目（轻表，不无限增长）
        cutoff = _now() - timedelta(days=COLLECT_RUNS_KEEP_DAYS)
        db.execute(delete(MarketEventCollectRun).where(MarketEventCollectRun.created_at < cutoff))
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("market_event_collect_runs 记账失败（不影响采集结果）", exc_info=True)


def list_collect_runs(db: Session, *, limit: int = 20) -> list[MarketEventCollectRun]:
    """P1-7：最近采集记录（新→旧）。"""
    return db.execute(
        select(MarketEventCollectRun).order_by(desc(MarketEventCollectRun.id)).limit(limit)
    ).scalars().all()


def recent_collects_all_empty(db: Session, *, lookback: int = 2) -> bool:
    """最近 lookback 次是否均为成功空采（不含全重复、失败或部分失败）。

    历史记录不足 lookback 条时返回 False——没有足够证据就不告警。
    """
    runs = db.execute(
        select(MarketEventCollectRun)
        .order_by(desc(MarketEventCollectRun.id))
        .limit(lookback)
    ).scalars().all()
    if len(runs) < lookback:
        return False
    return all(run.status == "done" and run.collected == 0 and run.skipped_duplicate == 0
               and run.errors_json in (None, "[]") for run in runs)


def collect_market_events(
    db: Session, payload: MarketEventCollectRequest, *, trigger_source: str = "manual"
) -> MarketEventCollectResponse:
    """P1-7：外层包一层采集运行账目（成功/失败都落一行 market_event_collect_runs）。

    账目写入失败只记日志不阻断采集（旧库未跑迁移时采集仍可用）。
    """
    started_at = _now()
    try:
        result = _collect_market_events_inner(db, payload)
    except Exception as exc:
        db.rollback()
        _record_collect_run(db, trigger_source=trigger_source, payload=payload,
                            collected=0, skipped=0, status="failed",
                            errors=[f"{type(exc).__name__}: {exc}"], started_at=started_at)
        raise
    _record_collect_run(db, trigger_source=trigger_source, payload=payload,
                        collected=result.collected, skipped=result.skipped_duplicate,
                        status=result.status, errors=result.errors, started_at=started_at)
    return result


def _collect_market_events_inner(db: Session, payload: MarketEventCollectRequest) -> MarketEventCollectResponse:
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

    # Fetch outside the lock, then serialize the entire read/dedup/write/commit
    # section. No schema migration or removal of legacy duplicate rows needed.
    with market_event_write_lock(db):
        if db.new or db.dirty or db.deleted:
            raise ValueError("Collection requires a session without pending changes")
        # Discard any earlier read snapshot (notably MySQL REPEATABLE READ).
        # A lock alone cannot make an old transaction see the previous writer.
        db.rollback()
        return _store_market_events(db, payload, all_raw, errors)


def _store_market_events(db: Session, payload: MarketEventCollectRequest,
                         all_raw: list[dict], errors: list[str]) -> MarketEventCollectResponse:
    cutoff = _now() - timedelta(days=payload.days)
    # P0-4：先自愈回填历史行，再用本批候选 hash 做索引查询去重，
    # 替代原先"全表拉所有标题"的内存 MD5 集合（表越大采集越慢）。
    _backfill_title_hashes(db)
    candidate_titles: dict[str, str] = {}
    for raw in all_raw:
        title = str(raw.get("title") or "").strip()
        if title:
            candidate_titles.setdefault(_title_hash(title), title)
    seen_hashes = _existing_title_hashes(db, set(candidate_titles))
    sector_index = build_index(resolve_concepts(db))
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
        classification = _analyze_classification(title,summary)
        impact_scope = classification["current_scope"]
        importance_level = classification["current_level"]
        sentiment = classification["current_sentiment"]
        expires_days = 14 if importance_level >= 4 else 7 if importance_level >= 3 else 3
        # P0-3：板块标签入库（逗号分隔板块 id），激活原先闲置的 affected_sectors 字段
        # P2-12：匹配前先合并 DB 关键词覆盖（后台可调词，无需发版）
        sectors = map_sectors_on(sector_index, title, summary)

        db.add(MarketEvent(
            title=title,
            summary=summary,
            impact_scope=impact_scope,
            importance_level=importance_level,
            affected_market=_affected_market(impact_scope),
            affected_sectors=",".join(sectors),
            sector_tag_source="auto",
            sentiment=sentiment,
            source=raw.get("source") or "unknown",
            source_url=raw.get("source_url"),
            is_manual=0,
            published_at=published_at,
            expires_at=published_at + timedelta(days=expires_days) if published_at else None,
            title_hash=title_hash,
        ))
        collected += 1

    db.commit()
    status = "failed" if errors and len(errors) >= len(payload.sources) else "partial" if errors else "done"
    return MarketEventCollectResponse(collected=collected, skipped_duplicate=skipped, errors=errors, status=status)


def cleanup_expired_market_events(db: Session, *, keep_days: int = CLEANUP_KEEP_DAYS_DEFAULT) -> "MarketEventCleanupResponse":
    """P0-1：清理过期消息（此前只有前端视觉过滤，数据库无限增长）。

    数据保护规则（硬约束，回归用例锁死）：
    1. 仅删除已过期且发布时间早于保留窗口的行；实际至少保留 365 天，
       兼容旧 90 天排程并保护一年热点历史；
    2. 排除被主题催化剂（ThemeCatalyst）引用的事件行，组合准入链路依赖该外键；
    3. 排除手工创建（is_manual=1）的行 —— 人工录入内容不自动删除，由人工管理；
    4. expires_at / published_at 为 NULL 的行不删（NULL 比较为假，保守保留）。
    """
    from app.schemas.market_event import MarketEventCleanupResponse

    keep_days = effective_keep_days(keep_days)
    now = _now()
    published_cutoff = now - timedelta(days=keep_days)
    referenced = select(ThemeCatalyst.market_event_id).where(ThemeCatalyst.market_event_id.is_not(None))
    stmt = select(MarketEvent).where(
        MarketEvent.expires_at.is_not(None),
        MarketEvent.expires_at < now,
        MarketEvent.published_at.is_not(None),
        MarketEvent.published_at < published_cutoff,
        MarketEvent.is_manual == 0,
        ~MarketEvent.id.in_(referenced),
    )
    stale = db.execute(stmt).scalars().all()
    for event in stale:
        db.delete(event)
    db.commit()
    logger.info("market_events cleanup: deleted %d rows (keep_days=%d)", len(stale), keep_days)
    return MarketEventCleanupResponse(deleted=len(stale), keep_days=keep_days, cutoff_date=published_cutoff)


# 单个板块返回给前端的样本事件 id 上限（防止 365 天窗口下 payload 膨胀）
HOTSPOT_EVENT_IDS_CAP = 50

# AI 事件归因（P1-8）：注入 AI 上下文的同期重大事件筛选口径
CONTEXT_EVENT_DAYS = 14
CONTEXT_EVENT_MIN_LEVEL = 4
CONTEXT_EVENT_LIMIT = 8


def recent_market_events_for_context(
    db: Session,
    *,
    days: int = CONTEXT_EVENT_DAYS,
    min_level: int = CONTEXT_EVENT_MIN_LEVEL,
    limit: int = CONTEXT_EVENT_LIMIT,
) -> list[dict]:
    """P1-8：取近期重大市场事件供 AI 上下文归因引用。

    只取 importance>=4（默认）的宏观/行业级事件，量级可控；调用方必须以
    is_untrusted 标记注入——AI 只能引用其解释异动，不得当作事实依据。
    """
    cutoff = _now() - timedelta(days=days)
    rows = db.execute(
        select(MarketEvent)
        .where(MarketEvent.importance_level >= min_level, MarketEvent.published_at >= cutoff)
        .order_by(desc(MarketEvent.importance_level), desc(MarketEvent.published_at))
        .limit(limit)
    ).scalars().all()
    return [
        {
            "id": event.id,
            "title": event.title,
            "impact_scope": event.impact_scope,
            "importance_level": event.importance_level,
            "sentiment": event.sentiment,
            "source": event.source,
            "published_at": event.published_at.isoformat() if event.published_at else None,
        }
        for event in rows
    ]


def get_market_event_hotspots(db: Session, *, days: int = 30) -> "MarketEventHotspotsResponse":
    """P0-3：概念热点后端聚合。

    口径说明：
    - 统计窗口按 published_at 回溯，不受列表筛选影响，也**不再剔除已过期消息**
      （过期只影响列表展示；此前前端把 365 天窗口当 30 天数据过滤是失真来源）；
    - 落库的 affected_sectors 优先；历史空值行用知识库现算（不回写，保持读时兼容）；
    - MySQL 5.7 兼容：无窗口函数/CTE，窗口过滤后在应用层聚合。
    """
    from app.schemas.market_event import (
        MarketEventHotspotSector,
        MarketEventHotspotsResponse,
    )

    cutoff = _now() - timedelta(days=days)
    rows = db.execute(
        select(
            MarketEvent.id,
            MarketEvent.title,
            MarketEvent.summary,
            MarketEvent.sentiment,
            MarketEvent.importance_level,
            MarketEvent.affected_sectors,
        ).where(MarketEvent.published_at >= cutoff)
    ).all()

    sector_index = build_index(resolve_concepts(db)) if any(row.affected_sectors is None for row in rows) else {}
    agg: dict[str, dict] = {}
    for event_id, title, summary, sentiment, level, sectors_field in rows:
        sector_ids = event_sector_ids(sectors_field, title or "", summary, sector_index)
        for sector_id in sector_ids:
            bucket = agg.setdefault(
                sector_id, {"bull": 0, "bear": 0, "neutral": 0, "max_level": 0, "event_ids": []}
            )
            if sentiment == "positive":
                bucket["bull"] += 1
            elif sentiment == "negative":
                bucket["bear"] += 1
            else:
                bucket["neutral"] += 1
            bucket["max_level"] = max(bucket["max_level"], level or 0)
            if len(bucket["event_ids"]) < HOTSPOT_EVENT_IDS_CAP:
                bucket["event_ids"].append(event_id)

    sectors = [
        MarketEventHotspotSector(
            sector_id=sector_id,
            bull=data["bull"],
            bear=data["bear"],
            neutral=data["neutral"],
            total=data["bull"] + data["bear"] + data["neutral"],
            max_level=data["max_level"],
            event_ids=data["event_ids"],
        )
        for sector_id, data in agg.items()
    ]
    # 与前端原排序口径一致：总提及量降序，再按最高重要级降序
    sectors.sort(key=lambda item: (item.total, item.max_level), reverse=True)
    return MarketEventHotspotsResponse(days=days, generated_at=_now(), sectors=sectors)
