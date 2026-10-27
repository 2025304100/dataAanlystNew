"""白盒测试 - 行情消息 P2-9：news 评分桶接真数据（2026-10-25）。

契约：
- compute_news_event_score：无快照→None；置信度不足→None；message_score 线性映射 0-100；
- _resolve_news_score：双开关全关零查询零行为；SHADOW 只出影子值；ENABLED 出生效值；
- _aggregate_final_scores：news_override 改变 priority（按 news 权重），None 时与旧公式一致。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.news_event import NewsEvent, NewsSnapshot
from app.models.symbol import Symbol
from app.schemas.news import NewsUpdateRequest
from app.services.news import (
    NEWS_SCORE_MIN_CONFIDENCE,
    _summarize_symbol,
    compute_news_event_score,
)
from app.services.scoring_config_engine import (
    _aggregate_final_scores,
    _resolve_news_score,
)

pytestmark = pytest.mark.whitebox


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _symbol(db):
    sym = Symbol(symbol="601398", name="测试银行", asset_type="stock", market="cn",
                 board="main", is_st=0, is_active=1)
    db.add(sym)
    db.commit()
    db.refresh(sym)
    return sym


def _snapshot(db, symbol_id, *, message_score, confidence, scope="symbol"):
    snap = NewsSnapshot(
        symbol_id=symbol_id, scope=scope,
        macro_score=0.0, sector_score=0.0, symbol_score=message_score,
        message_score=message_score, sentiment="positive", risk_level="low",
        confidence=confidence, positive_count=3, negative_count=0, risk_count=0,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


# ----------------------------------------------------------------------------
# compute_news_event_score
# ----------------------------------------------------------------------------

def test_compute_news_score_none_without_snapshot(db_session):
    sym = _symbol(db_session)
    assert compute_news_event_score(db_session, sym.id) is None


def test_compute_news_score_linear_mapping(db_session):
    sym = _symbol(db_session)
    _snapshot(db_session, sym.id, message_score=10.0, confidence=0.8)
    assert compute_news_event_score(db_session, sym.id) == 75.0
    _snapshot(db_session, sym.id, message_score=-20.0, confidence=0.8)  # 更新的一条
    assert compute_news_event_score(db_session, sym.id) == 0.0


def test_compute_news_score_low_confidence_returns_none(db_session):
    sym = _symbol(db_session)
    _snapshot(db_session, sym.id, message_score=15.0, confidence=NEWS_SCORE_MIN_CONFIDENCE - 0.01)
    assert compute_news_event_score(db_session, sym.id) is None


def test_compute_news_score_takes_latest_snapshot(db_session):
    sym = _symbol(db_session)
    _snapshot(db_session, sym.id, message_score=8.0, confidence=0.9)
    _snapshot(db_session, sym.id, message_score=-4.0, confidence=0.9)  # 后插入=更新
    assert compute_news_event_score(db_session, sym.id) == 40.0


def test_compute_news_score_reads_snapshot_written_by_summarize(db_session):
    """回归：生产链路 update_news(scope="symbols") 写入的快照必须能被读到。

    NewsSnapshot.scope 记录的是请求范围而非快照粒度，历史实现按 scope=="symbol"
    过滤会永远查不到生产写入的行，导致灰度开关静默失效。
    """
    sym = _symbol(db_session)
    events = [
        NewsEvent(
            title="利好一", source="cninfo", event_type="news", strength=1,
            sentiment="positive", risk_level="low", effective_score=5.0,
            published_at=_now(), created_at=_now(),
        ),
        NewsEvent(
            title="利好二", source="cninfo", event_type="news", strength=1,
            sentiment="positive", risk_level="low", effective_score=5.0,
            published_at=_now(), created_at=_now(),
        ),
    ]
    payload = NewsUpdateRequest(scope="symbols", symbol_ids=[sym.id])
    summary = _summarize_symbol(db_session, payload, sym, events)
    db_session.commit()
    assert summary.confidence >= NEWS_SCORE_MIN_CONFIDENCE
    # message_score=10 → 50 + 10*2.5 = 75
    assert compute_news_event_score(db_session, sym.id) == 75.0


# ----------------------------------------------------------------------------
# 灰度开关
# ----------------------------------------------------------------------------

def test_resolve_news_score_all_off_short_circuits(db_session, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "news_score_enabled", False, raising=False)
    monkeypatch.setattr(settings, "news_score_shadow", False, raising=False)
    sym = _symbol(db_session)
    _snapshot(db_session, sym.id, message_score=10.0, confidence=0.9)
    # 双关：即使有快照也返回 (None, None)，不触发查询路径
    assert _resolve_news_score(db_session, sym.id) == (None, None)


def test_resolve_news_score_shadow_vs_enabled(db_session, monkeypatch):
    from app.core.config import settings

    sym = _symbol(db_session)
    _snapshot(db_session, sym.id, message_score=10.0, confidence=0.9)

    monkeypatch.setattr(settings, "news_score_enabled", False, raising=False)
    monkeypatch.setattr(settings, "news_score_shadow", True, raising=False)
    assert _resolve_news_score(db_session, sym.id) == (None, 75.0)

    monkeypatch.setattr(settings, "news_score_enabled", True, raising=False)
    assert _resolve_news_score(db_session, sym.id) == (75.0, 75.0)


# ----------------------------------------------------------------------------
# 聚合公式
# ----------------------------------------------------------------------------

_CONFIG = {
    "final_weights": {"quality": 0.4, "timing": 0.5, "news": 0.1},
    "dimensions": [],
}


def test_aggregate_news_override_changes_priority():
    dims = {"trend": 60.0}
    base = _aggregate_final_scores(dims, _CONFIG, {})
    boosted = _aggregate_final_scores(dims, _CONFIG, {}, news_override=90.0)
    # news 权重 0.1：(90-50)*0.1 = +4 到 priority
    assert boosted[2] == pytest.approx(base[2] + 4.0, abs=0.01)


def test_aggregate_news_override_none_matches_legacy():
    dims = {"trend": 60.0}
    a = _aggregate_final_scores(dims, _CONFIG, {}, news_override=None)
    b = _aggregate_final_scores(dims, _CONFIG, {})
    assert a == b
