"""白盒测试 - 行情消息 P1-8：AI 事件归因注入（2026-10-25）。

契约：
- recent_market_events_for_context 只取 importance>=min_level 且窗口内的事件，按级别/时间排序；
- context_pack._build_untrusted_content 在个股新闻之外追加 market_event 条目（is_untrusted=True），
  无 symbol_id 时同样注入（组合级上下文）；
- 既有新闻注入行为不受影响（无 MarketEvent 数据时输出与旧版一致）。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.market_event import MarketEvent
from app.services.market_events import (
    _now,
    recent_market_events_for_context,
)

pytestmark = pytest.mark.whitebox


def _md5(title: str) -> str:
    import hashlib
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def _mk(db, *, title, level, days_ago=0):
    db.add(MarketEvent(
        title=title, impact_scope="macro_policy", importance_level=level,
        affected_market="A股/期货", sentiment="neutral", source="cctv",
        is_manual=0, published_at=_now() - timedelta(days=days_ago),
        expires_at=_now() + timedelta(days=3), title_hash=_md5(title),
    ))
    db.commit()


def test_recent_market_events_filters_level_and_window(db_session):
    _mk(db_session, title="美联储紧急降息五十年未见", level=5)
    _mk(db_session, title="国常会部署一揽子增量政策", level=4)
    _mk(db_session, title="普通行业例行会议纪要", level=2)          # 级别不足
    _mk(db_session, title="上月的重要会议已经过去", level=5, days_ago=30)  # 窗口外

    events = recent_market_events_for_context(db_session, days=14, min_level=4)
    titles = [e["title"] for e in events]
    assert titles == ["美联储紧急降息五十年未见", "国常会部署一揽子增量政策"]  # 级别优先
    assert all(e["published_at"] for e in events)


def test_context_pack_injects_market_events_untrusted(db_session):
    from app.services.ai.context_pack import _build_untrusted_content

    _mk(db_session, title="央行宣布全面降准0.5个百分点", level=5)
    items = _build_untrusted_content(db_session, {"symbol_id": None})
    market_items = [i for i in items if i["type"] == "market_event"]
    assert len(market_items) == 1
    assert market_items[0]["title"] == "央行宣布全面降准0.5个百分点"
    assert market_items[0]["is_untrusted"] is True


def test_context_pack_market_events_coexist_with_news(db_session):
    from app.models.news_event import NewsEvent
    from app.models.symbol import Symbol
    from app.services.ai.context_pack import _build_untrusted_content

    sym = Symbol(symbol="600019", name="测试股", asset_type="stock", market="cn",
                 board="main", is_st=0, is_active=1)
    db_session.add(sym)
    db_session.commit()
    db_session.add(NewsEvent(
        symbol_id=sym.id, symbol="600019", title="个股利好新闻", source="eastmoney-news",
        event_type="news", sentiment="positive", strength=2,
        raw_score=5.0, effective_score=4.0, risk_level="low", published_at=_now(),
    ))
    _mk(db_session, title="国务院发布资本市场改革意见", level=5)
    db_session.commit()

    items = _build_untrusted_content(db_session, {"symbol_id": sym.id})
    by_type = {i["type"] for i in items}
    assert "news" in by_type and "market_event" in by_type
    assert all(i["is_untrusted"] is True for i in items)
    # 无事件数据时，纯个股场景输出退化为旧行为（只有 news）
    db_session.query(MarketEvent).delete()
    db_session.commit()
    items2 = _build_untrusted_content(db_session, {"symbol_id": sym.id})
    assert {i["type"] for i in items2} == {"news"}
