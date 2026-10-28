"""Bounded source enrichment and user-requested data preparation."""
import io
import json
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests
from fastapi import HTTPException
from sqlalchemy import select

from app.models.market_event import MarketEvent
from app.models.market_signal import MarketSignalEvidence
from app.services import market_signal_sources as sources
from app.services.market_event_collection_lock import market_event_write_lock

PUBLIC_NEWS_DOMAINS = ("gov.cn", "eastmoney.com", "caixin.com", "cctv.com", "cninfo.com.cn",
    "sse.com.cn", "szse.cn", "shmet.com", "sina.com.cn", "cls.cn")
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def sync_required_data(db, rule_ids=None):
    """Prepare only missing prerequisites; never refresh a ready target implicitly."""
    from app.models.market_signal import MarketSignalRule
    from app.models.symbol import Symbol
    from app.models.universe import UniverseSymbol
    from app.services.market_signal_quotes import CHINA, QuoteUnavailable, quote_context
    query = select(MarketSignalRule).where(MarketSignalRule.enabled.is_(True))
    if rule_ids:
        query = query.where(MarketSignalRule.id.in_(rule_ids))
    configs = [(row.id, json.loads(row.config_json)) for row in db.scalars(query.limit(30))]
    items, calendar_attempted = [], False
    for rule_id, config in configs:
        try:
            context = quote_context(db, config, sources.utcnow())
        except QuoteUnavailable:
            context = None
        if context:
            items.append({"rule_id": rule_id, "status": "ready", "context": context})
            continue
        try:
            if not calendar_attempted:
                from app.models.market_data import MarketCalendar
                today = (sources.utcnow()+CHINA).date()
                complete = all(db.get(MarketCalendar, today+timedelta(days=offset)) is not None
                               for offset in range(20))
                if not complete:
                    from app.services.trade_calendar_sync import refresh_cn_trade_calendar
                    db.commit()
                    refresh_cn_trade_calendar(force=False)
                    db.expire_all()
                calendar_attempted = True
            end = (sources.utcnow()+CHINA).date()
            start = end-timedelta(days=540)
            if config.get("quote_type") == "index":
                from app.services.index_data import sync_index_daily
                sync_index_daily(db, config["quote_key"], start_date=start, end_date=end)
            else:
                key = config.get("quote_key") or config["target_key"]
                symbol = db.scalar(select(Symbol).where(Symbol.symbol == key))
                if symbol:
                    from app.services.market_data import sync_symbol_daily_bars
                    sync_symbol_daily_bars(db, symbol, start_date=start, end_date=end, adjust="qfq")
                else:
                    symbol = db.scalar(select(UniverseSymbol).where(UniverseSymbol.symbol == key))
                    if symbol is None:
                        raise ValueError("目标已不在标的库")
                    from app.services.universe_sync import repair_one_universe_symbol
                    repair_one_universe_symbol(db, symbol, start_date=start, end_date=end, chunk_days=90)
            db.commit()
            context = quote_context(db, config, sources.utcnow())
            items.append({"rule_id": rule_id, "status": "ready", "context": context})
        except Exception as exc:
            db.rollback()
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            items.append({"rule_id": rule_id, "status": "blocked", "reason": message[:350]})
    return {"items": items, "partial": any(item["status"] != "ready" for item in items)}


def monitor(db, *, analyze=None, progress=None):
    """Collect, prepare, analyze, queue, dispatch and settle through real services."""
    from app.models.app_setting import AppSetting
    from app.models.market_signal import MarketSignalRule
    from app.schemas.market_signal import SignalRunRequest
    from app.services.market_signal_engine import run
    from app.services.market_signals import get_settings, _json
    from app.services.market_signal_quotes import CHINA
    from app.services.notifications.dispatcher import Dispatcher
    from datetime import time
    with market_event_write_lock(db, namespace="market-signals-monitor"):
        db.rollback()
        settings, now = get_settings(db), sources.utcnow()
        if not settings["enabled"]:
            return run(db, SignalRunRequest(analyze=False))
        state = db.get(AppSetting, "market_signals.runtime")
        runtime = json.loads(state.value_json) if state else {}
        collected, data, errors = None, None, []
        if progress:
            progress("collect", 15, "正在采集关注消息")
        last_collect = sources.utc(runtime.get("last_collect_at"))
        if settings["collect_on_monitor"] and (not last_collect or now-last_collect >= timedelta(minutes=30)):
            collected = collect_followed_news(db, int(runtime.get("symbol_offset", 0)))
            errors.extend(collected["errors"])
            runtime.update(last_collect_at=now.isoformat(), symbol_offset=collected["next_offset"], collection=collected)
        if progress:
            progress("prepare", 40, "检查日历和目标行情")
        local = now+CHINA
        last_sync = sources.utc(runtime.get("last_data_sync_at"))
        if not time(9, 30) <= local.time() < time(15) and (not last_sync or now-last_sync >= timedelta(hours=1)):
            data = sync_required_data(db)
            runtime.update(last_data_sync_at=now.isoformat(), data=data)
        if state is None:
            state = AppSetting(key="market_signals.runtime", updated_by="market-signals-monitor")
            db.add(state)
        state.value_json, state.updated_at = _json(runtime), now
        db.commit()
        if progress:
            progress("analyze", 60, "核验原文并分析新增事实")
        result = run(db, SignalRunRequest(analyze=settings["auto_analyze"] if analyze is None else analyze))
        if progress:
            progress("deliver", 90, "发送前再次检查规则与消息时效")
        db.commit()
        dispatched = Dispatcher().run_once()
        return {**result, "collection": collected, "data": data, "dispatched": dispatched,
                "errors": errors, "partial": bool(errors) or bool(data and data["partial"])}


def collect_followed_news(db, offset=0):
    from app.models.market_signal import MarketSignalRule
    from app.schemas.market_event import MarketEventCollectRequest
    from app.services import market_events
    from app.services.market_signals import _json
    errors, keys, raw = [], [], []
    for row in db.scalars(select(MarketSignalRule).where(MarketSignalRule.enabled.is_(True)).order_by(MarketSignalRule.id)):
        config = json.loads(row.config_json)
        if row.target_type == "symbol" and row.target_key not in keys:
            keys.append(row.target_key)
    db.commit()
    try:
        macro = market_events.collect_market_events(db, MarketEventCollectRequest(
            sources=["eastmoney-global", "caixin"], days=2), trigger_source="market-signals-monitor")
        errors.extend(macro.errors)
    except Exception as exc:
        db.rollback()
        errors.append(f"宏观来源暂不可用：{str(exc)[:180]}")
    chosen = (keys[offset:] + keys[:offset])[:5] if keys else []
    for key in chosen:
        for source, fetcher, kwargs in (
            ("stock_news_em", market_events.ak.stock_news_em, {"symbol": key}),
            ("stock_zh_a_disclosure_report_cninfo", market_events.ak.stock_zh_a_disclosure_report_cninfo,
             {"symbol": key, "market": "沪深京",
              "start_date": (sources.utcnow()-timedelta(days=2)).strftime("%Y%m%d"),
              "end_date": (sources.utcnow()+timedelta(hours=8)).strftime("%Y%m%d")}),
        ):
            try:
                entries = market_events._fetch_from_ak(fetcher, source, 15, **kwargs)
                for item in entries:
                    item["symbol_codes"] = [key]
                raw.extend(entries)
            except Exception:
                errors.append(f"{source}/{key} 采集失败，保留已有证据，下轮重试")
    with market_event_write_lock(db):
        db.rollback()
        for item in raw:
            sources.capture_evidence(db, item)
        db.commit()
    # Retrieve up to two recent linked articles when the feed only supplied a
    # headline. Content revisions remain separate immutable evidence records.
    candidates = list(db.scalars(select(MarketSignalEvidence).where(
        MarketSignalEvidence.first_seen_at >= sources.utcnow()-timedelta(hours=24)
    ).order_by(MarketSignalEvidence.id.desc()).limit(30)))
    parents = {json.loads(row.metadata_json).get("parent_evidence_id") for row in candidates}
    enriched = 0
    for row in candidates:
        if enriched >= 2:
            break
        if row.id in parents or len(row.text) >= 120 or not row.source_url:
            continue
        try:
            _approved_url(row.source_url)
        except HTTPException:
            continue
        try:
            enriched += 1
            enrich_evidence(db, row.id)
        except HTTPException:
            pass  # The original evidence remains analyzable; missing facts block delivery.
    return {"stock_targets": chosen, "captured": len(raw), "enriched": enriched, "errors": errors,
            "next_offset": (offset+len(chosen)) % len(keys) if keys else 0}


def _approved_url(url):
    try:
        parsed = urlsplit(url or "")
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(422, "原文链接格式无效") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password
            or port not in (None, 80, 443)
            or not any(host == domain or host.endswith("."+domain) for domain in PUBLIC_NEWS_DOMAINS)):
        raise HTTPException(422, "仅提取已支持的公开政策、交易所及财经来源；其他来源请粘贴可核验原文")
    return url


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.all_text, self.article_text = [], []
        self.hidden = 0
        self.article_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "header", "footer", "noscript", "svg"):
            self.hidden += 1
        if tag in ("article", "main"):
            self.article_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "header", "footer", "noscript", "svg"):
            self.hidden = max(0, self.hidden-1)
        if tag in ("article", "main"):
            self.article_depth = max(0, self.article_depth-1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.all_text.append(data.strip())
            if self.article_depth:
                self.article_text.append(data.strip())


def enrich_evidence(db, evidence_id):
    evidence = db.get(MarketSignalEvidence, evidence_id)
    if evidence is None:
        raise HTTPException(404, "原始消息不存在")
    url = _approved_url(evidence.source_url)
    content, content_type, encoding = b"", "", "utf-8"
    try:
        for _ in range(4):
            with requests.get(url, timeout=(5, 12), stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = _approved_url(urljoin(url, response.headers.get("Location", "")))
                    continue
                response.raise_for_status()
                if int(response.headers.get("Content-Length", "0") or 0) > MAX_SOURCE_BYTES:
                    raise HTTPException(422, "原文超过2MB，请补充关键条款原文")
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=16384):
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise HTTPException(422, "原文超过2MB，请补充关键条款原文")
                    chunks.append(chunk)
                content = b"".join(chunks)
                content_type = response.headers.get("Content-Type", "").lower()
                encoding = response.encoding or "utf-8"
                break
        else:
            raise HTTPException(422, "原文重定向次数过多")
    except requests.RequestException as exc:
        raise HTTPException(502, "原始来源暂时不可读取，请稍后重试或补充原文") from exc
    if content.startswith(b"%PDF") or "application/pdf" in content_type:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted or len(reader.pages) > 40:
                raise ValueError("原文加密或页数过多")
            text = "\n".join(page.extract_text() or "" for page in list(reader.pages)[:8])
        except ImportError as exc:
            raise HTTPException(503, "PDF提取依赖未安装，请按 requirements.txt 安装后重试") from exc
        except Exception as exc:
            raise HTTPException(422, "PDF原文无法可靠提取，请粘贴可核验条款文字") from exc
    else:
        parser = _ArticleParser()
        parser.feed(content.decode(encoding, errors="replace"))
        text = "\n".join(parser.article_text or parser.all_text)
    if len(text.strip()) < 10:
        raise HTTPException(422, "原文不足或为扫描图片，请补充可核验文字")
    raw = {"title": evidence.title, "full_text": text[:40000], "source": evidence.source,
        "source_url": url, "published_at": evidence.published_at, "time_quality": evidence.time_quality,
        "kind": evidence.kind, "parent_evidence_id": evidence.id,
        **{key: json.loads(evidence.metadata_json).get(key, []) for key in ("sector_ids", "symbol_codes")}}
    with market_event_write_lock(db):
        db.rollback()
        row = sources.capture_evidence(db, raw, origin="article")
        db.commit()
        return sources.evidence_read(row)


def import_legacy(db):
    with market_event_write_lock(db):
        db.rollback()
        rows = list(db.scalars(select(MarketEvent).order_by(MarketEvent.id.desc()).limit(200)))
        for row in rows:
            sources.capture_evidence(db, {"title": row.title, "summary": row.summary,
                "source": row.source, "source_url": row.source_url, "published_at": None,
                "time_quality": "legacy_unknown", "legacy_market_event_id": row.id,
                "sector_ids": [key for key in (row.affected_sectors or "").split(",") if key],
                "symbol_codes": [key for key in (row.affected_symbols or "").split(",") if key]}, origin="legacy")
        db.commit()
    return {"imported": len(rows), "message": "已导入最近200条以内消息；历史时间来源未明的消息需补充发布时间，不自动提醒"}
