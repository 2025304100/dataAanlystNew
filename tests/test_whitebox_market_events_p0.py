"""白盒测试 - 行情消息模块 P0 落地（2026-10-25）。

覆盖四个 P0 的核心契约：
- P0-1 过期清理：仅删"已过期+发布超保留窗口+自动采集"行；
  被主题催化剂引用的行 / 手工行 / 窗口内行 / NULL 时间行一律保留。
- P0-4 去重落库：title_hash 写入、二次采集命中跳过、存量 NULL 行采集时自愈回填。
- P0-3 热点聚合：落库板块优先、历史空值用知识库现算、时间窗口过滤、计数正确。
- P0-2 调度接线：任务类型/payload 校验/默认排程注册。
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from app.models.investment_theme import InvestmentTheme, ThemeCatalyst
from app.models.market_event import MarketEvent
from app.schemas.market_event import (
    MarketEventCollectRequest,
    MarketEventCreate,
)
from app.services.market_events import (
    _now,
    cleanup_expired_market_events,
    collect_market_events,
    create_market_event,
    get_market_event_hotspots,
)
from app.services.sector_concepts import map_to_sectors, parse_affected_sectors
from app.services.scheduled_tasks import (
    DEFAULT_SCHEDULES,
    TASK_DEFINITIONS,
    validate_task_payload,
)

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _md5(title: str) -> str:
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def _make_event(db, *, title: str, published_at, expires_at, is_manual: int = 0,
                sentiment: str = "neutral", level: int = 3, affected_sectors: str | None = None):
    event = MarketEvent(
        title=title,
        summary=None,
        impact_scope="other",
        importance_level=level,
        affected_market="A股/期货",
        affected_sectors=affected_sectors,
        sentiment=sentiment,
        source="test",
        source_url=None,
        is_manual=is_manual,
        published_at=published_at,
        expires_at=expires_at,
        title_hash=_md5(title),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


# ----------------------------------------------------------------------------
# P0-4：去重落库
# ----------------------------------------------------------------------------

def test_collect_writes_title_hash_and_dedups_on_second_run(db_session, monkeypatch):
    titles = ["央行宣布下调金融机构存款准备金率", "国务院常务会议部署稳增长政策"]
    fake_rows = [
        {"title": t, "summary": None, "source": "eastmoney-global",
         "source_url": None, "published_at": _now()}
        for t in titles
    ]
    monkeypatch.setattr("app.services.market_events._fetch_global_news", lambda: list(fake_rows))

    first = collect_market_events(
        db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"])
    )
    assert first.collected == 2
    rows = db_session.query(MarketEvent).order_by(MarketEvent.id).all()
    assert {row.title_hash for row in rows} == {_md5(t) for t in titles}

    second = collect_market_events(
        db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"])
    )
    assert second.collected == 0
    assert second.skipped_duplicate == 2
    assert db_session.query(MarketEvent).count() == 2


def test_collect_backfills_legacy_rows_without_hash(db_session, monkeypatch):
    legacy = MarketEvent(
        title="存量历史消息（无hash）", impact_scope="other", importance_level=2,
        affected_market="A股/期货", sentiment="neutral", source="legacy",
        is_manual=0, published_at=_now() - timedelta(days=1),
        expires_at=_now() + timedelta(days=1), title_hash=None,
    )
    db_session.add(legacy)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.market_events._fetch_global_news",
        lambda: [{"title": "一条全新的消息标题甲", "summary": None, "source": "eastmoney-global",
                  "source_url": None, "published_at": _now()}],
    )
    result = collect_market_events(
        db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"])
    )
    assert result.collected == 1
    db_session.refresh(legacy)
    assert legacy.title_hash == _md5("存量历史消息（无hash）")
    # 再次采集存量标题应被回填后的 hash 拦下，而不是重复入库
    monkeypatch.setattr(
        "app.services.market_events._fetch_global_news",
        lambda: [{"title": "存量历史消息（无hash）", "summary": None, "source": "eastmoney-global",
                  "source_url": None, "published_at": _now()}],
    )
    again = collect_market_events(
        db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"])
    )
    assert again.collected == 0
    assert again.skipped_duplicate == 1


def test_collect_request_defaults_to_all_sources():
    request = MarketEventCollectRequest()
    assert set(request.sources) == {
        "eastmoney-global", "caixin", "cctv", "baidu", "baidu-report", "futures-shmet",
    }


# ----------------------------------------------------------------------------
# P0-3：板块标签落库 + 热点聚合
# ----------------------------------------------------------------------------

def test_collect_writes_affected_sectors(db_session, monkeypatch):
    title = "台积电2nm晶圆厂光刻设备如期交付"
    monkeypatch.setattr(
        "app.services.market_events._fetch_global_news",
        lambda: [{"title": title, "summary": None, "source": "eastmoney-global",
                  "source_url": None, "published_at": _now()}],
    )
    collect_market_events(db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"]))
    row = db_session.query(MarketEvent).filter(MarketEvent.title == title).one()
    expected = ",".join(map_to_sectors(title, None))
    assert row.affected_sectors == (expected or None)
    assert parse_affected_sectors(row.affected_sectors) != []  # 该标题必须能匹配到板块


def test_hotspots_prefers_stored_sectors_and_falls_back_for_legacy(db_session):
    now = _now()
    # e1：落库板块 + 利多 + 4 级（新数据路径）
    _make_event(db_session, title="半导体设备利多消息", published_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=6), sentiment="positive", level=4,
                affected_sectors="semiconductor")
    # e2：历史行无 affected_sectors → 知识库现算（光伏组件 → solar_pv）
    _make_event(db_session, title="光伏组件出口订单环比回升", published_at=now - timedelta(days=2),
                expires_at=now + timedelta(days=5), sentiment="neutral", level=3,
                affected_sectors=None)
    # e3：窗口外的旧消息，不得计入 30 天聚合
    _make_event(db_session, title="黄金避险需求大增", published_at=now - timedelta(days=100),
                expires_at=now - timedelta(days=90), sentiment="positive", level=5,
                affected_sectors="precious_metals")

    result = get_market_event_hotspots(db_session, days=30)
    by_id = {item.sector_id: item for item in result.sectors}

    assert "semiconductor" in by_id
    assert by_id["semiconductor"].bull == 1
    assert by_id["semiconductor"].max_level == 4
    assert by_id["semiconductor"].event_ids  # 样本事件 id 可用于前端联动

    expected_fallback = map_to_sectors("光伏组件出口订单环比回升", None)
    assert expected_fallback, "测试标题应至少命中一个板块"
    for sector_id in expected_fallback:
        assert sector_id in by_id
        assert by_id[sector_id].neutral >= 1

    # 窗口外的 precious_metals 不应出现在 30 天聚合里
    assert "precious_metals" not in by_id

    # 排序契约：总提及量降序
    totals = [item.total for item in result.sectors]
    assert totals == sorted(totals, reverse=True)


def test_manual_create_sets_hash_and_auto_sectors(db_session):
    event = create_market_event(
        db_session, MarketEventCreate(title="央行黄金储备连续增持", importance_level=4)
    )
    assert event.title_hash == _md5("央行黄金储备连续增持")
    assert event.affected_sectors  # 自动打标非空
    assert set((event.affected_sectors or "").split(",")) <= {
        concept.id for concept in __import__(
            "app.services.sector_concepts", fromlist=["SECTOR_CONCEPTS"]
        ).SECTOR_CONCEPTS
    }


# ----------------------------------------------------------------------------
# P0-1：过期清理的数据保护规则
# ----------------------------------------------------------------------------

def test_cleanup_deletes_only_stale_auto_unreferenced_rows(db_session):
    now = _now()
    old = now - timedelta(days=400)      # 超出 365 天热点保护窗口
    expired = now - timedelta(days=1)    # 已过期

    should_delete = _make_event(
        db_session, title="自动采集的陈旧过期消息", published_at=old, expires_at=expired, is_manual=0,
    )
    referenced = _make_event(
        db_session, title="被主题催化剂引用的过期消息", published_at=old, expires_at=expired, is_manual=0,
    )
    manual = _make_event(
        db_session, title="手工录入的过期消息", published_at=old, expires_at=expired, is_manual=1,
    )
    in_window = _make_event(
        db_session, title="窗口内的过期消息", published_at=now - timedelta(days=10), expires_at=expired,
    )
    not_expired = _make_event(
        db_session, title="未过期的陈旧消息", published_at=old, expires_at=now + timedelta(days=1),
    )
    null_published = _make_event(
        db_session, title="无发布时间的过期消息", published_at=None, expires_at=expired,
    )

    theme = InvestmentTheme(code="t_cleanup", name="清理保护测试主题")
    db_session.add(theme)
    db_session.commit()
    db_session.add(ThemeCatalyst(
        theme_id=theme.id, market_event_id=referenced.id,
        title_snapshot=referenced.title, catalyst_score=80.0,
    ))
    db_session.commit()

    result = cleanup_expired_market_events(db_session, keep_days=90)

    assert result.deleted == 1
    remaining = {row.id for row in db_session.query(MarketEvent).all()}
    assert should_delete.id not in remaining
    assert referenced.id in remaining      # 催化剂外键保护
    assert manual.id in remaining          # 手工行保护
    assert in_window.id in remaining       # 最长 365 天回看窗口保护
    assert not_expired.id in remaining     # 未过期保护
    assert null_published.id in remaining  # NULL 时间保守保留


def test_cleanup_is_idempotent(db_session):
    now = _now()
    _make_event(
        db_session, title="幂等清理目标", published_at=now - timedelta(days=500),
        expires_at=now - timedelta(days=100),
    )
    first = cleanup_expired_market_events(db_session, keep_days=90)
    second = cleanup_expired_market_events(db_session, keep_days=90)
    assert first.deleted == 1
    assert second.deleted == 0


# ----------------------------------------------------------------------------
# P0-2：调度接线契约
# ----------------------------------------------------------------------------

def test_task_definitions_and_default_schedules_registered():
    assert "market_events_collect" in TASK_DEFINITIONS
    assert "market_events_cleanup" in TASK_DEFINITIONS
    schedule_types = {item["task_type"] for item in DEFAULT_SCHEDULES}
    assert {"market_events_collect", "market_events_cleanup"} <= schedule_types
    names = {item["name"] for item in DEFAULT_SCHEDULES}
    assert "每日行情消息采集" in names
    assert "每日行情消息过期清理" in names


def test_validate_collect_payload_rejects_unknown_source():
    normalized = validate_task_payload(
        "market_events_collect", {"days": 7, "sources": ["cctv", "caixin"]}
    )
    assert normalized == {"days": 7, "sources": ["cctv", "caixin"]}
    with pytest.raises(ValueError):
        validate_task_payload("market_events_collect", {"days": 7, "sources": ["not-a-source"]})
    with pytest.raises(ValueError):
        validate_task_payload("market_events_collect", {"days": 99})


def test_validate_cleanup_payload_bounds():
    assert validate_task_payload("market_events_cleanup", {}) == {"keep_days": 365}
    with pytest.raises(ValueError):
        # 低于 90 天的配置统一拒绝；旧 90 天配置有效执行窗口提升为 365 天。
        validate_task_payload("market_events_cleanup", {"keep_days": 7})
