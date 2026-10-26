"""白盒测试 - 行情消息 P1-5：打标质量提升（2026-10-25）。

契约：
- scope 判定按命中关键词数加权取最高（多证据分类胜出，消除词表顺序敏感）；
- 词表支持 data/config/market_event_keywords.json 覆盖（同名键整体替换，mtime 缓存）；
- sort_by=importance_level 时同级消息按来源权威性（SOURCE_CREDIBILITY）次级排序。
存量消息不受影响（只改增量采集的分类逻辑）。
"""
from __future__ import annotations

import json

import pytest

from app.models.market_event import MarketEvent
from app.services import market_events as svc

pytestmark = pytest.mark.whitebox


def _md5(title: str) -> str:
    import hashlib
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def _mk(db, *, title, level=3, source="cctv", sentiment="neutral", scope="macro_policy"):
    event = MarketEvent(
        title=title, impact_scope=scope, importance_level=level,
        affected_market="A股/期货", sentiment=sentiment, source=source,
        is_manual=0, published_at=svc._now(), expires_at=svc._now(),
        title_hash=_md5(title),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


# ----------------------------------------------------------------------------
# scope 加权计分
# ----------------------------------------------------------------------------

def test_scope_weighted_scoring_beats_dict_order():
    # 旧逻辑（首命中）："美国"先命中 international（若词序在前）；
    # 新逻辑：标题含"央行/降息"（macro_policy 多词命中）应胜出单命中的 international。
    title = "中国央行宣布降息，美国市场反应积极"
    scope = svc._classify_impact_scope(title, None)
    assert scope == "macro_policy"


def test_scope_no_match_falls_back_to_other():
    assert svc._classify_impact_scope("公司发布年度品牌宣传片亮相", None) == "other"


# ----------------------------------------------------------------------------
# 词表外置覆盖
# ----------------------------------------------------------------------------

def test_keyword_override_file_replaces_scope_words(tmp_path, monkeypatch):
    override = tmp_path / "market_event_keywords.json"
    override.write_text(json.dumps({
        "impact_scope": {"crypto_web3": ["狗狗币", "柴犬币"]},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(svc, "_KEYWORD_OVERRIDE_FILE", override)
    monkeypatch.setattr(svc, "_keyword_cache", {"mtime": None, "scope": None, "level": None, "sentiment": None})

    # 覆盖词命中（默认词表中不存在"狗狗币"）
    assert svc._classify_impact_scope("狗狗币单日暴涨创纪录", None) == "crypto_web3"
    # 未覆盖的默认词表仍在生效
    assert svc._classify_impact_scope("央行降息释放流动性", None) == "macro_policy"

    # 文件删除后回退默认（缓存 mtime 失效）
    override.unlink()
    assert svc._classify_impact_scope("狗狗币单日暴涨创纪录", None) == "other"


def test_keyword_override_corrupt_file_falls_back(tmp_path, monkeypatch):
    override = tmp_path / "bad.json"
    override.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(svc, "_KEYWORD_OVERRIDE_FILE", override)
    monkeypatch.setattr(svc, "_keyword_cache", {"mtime": None, "scope": None, "level": None, "sentiment": None})
    assert svc._classify_impact_scope("央行降息释放流动性", None) == "macro_policy"


# ----------------------------------------------------------------------------
# 信誉度次级排序
# ----------------------------------------------------------------------------

def test_importance_sort_uses_source_credibility(db_session):
    # 同级并列：央视(5) 应排在 futures-shmet(3) 与未知来源(默认2) 之前
    low = _mk(db_session, title="期货市场常规成交综述", level=5, source="futures-shmet", scope="commodity_futures")
    unknown = _mk(db_session, title="某自媒体汇总市场观点汇总", level=5, source="unknown-src")
    top = _mk(db_session, title="央视权威发布重大政策解读", level=5, source="cctv")

    result = svc.list_market_events(db_session, limit=10, sort_by="importance_level")
    ids = [event.id for event in result.events]
    assert ids.index(top.id) < ids.index(low.id) < ids.index(unknown.id)
