"""Real Chromium -> production frontend -> HTTP APIs -> isolated MySQL 5.7.

Happy paths do not mock APIs. One explicit network-abort test covers fallback.
Run through scripts/verify_market_news_acceptance.py, never a shared app URL.
"""
import csv
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.models.investment_theme import ThemeCatalyst
from app.models.market_event import MarketEvent, MarketEventCollectRun, SectorConceptConfig
from tests.market_news_acceptance_support import REV91, assert_identity, engine_for, isolated_url

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(
    not os.environ.get("MARKET_NEWS_E2E_MYSQL57_URL") and os.environ.get("REQUIRE_MARKET_NEWS_ACCEPTANCE") != "1",
    reason="Explicit isolated browser lane; run scripts/verify_market_news_acceptance.py")]
ORIGIN = os.environ.get("E2E_FRONTEND_URL", "")


@pytest.fixture
def case_db():
    url = isolated_url("MARKET_NEWS_E2E_MYSQL57_URL")
    engine = engine_for(url)
    with engine.begin() as connection:
        assert_identity(connection, url)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REV91
        connection.execute(delete(ThemeCatalyst).where(ThemeCatalyst.market_event_id.is_not(None)))
        connection.execute(delete(MarketEvent))
        connection.execute(delete(MarketEventCollectRun))
        connection.execute(delete(SectorConceptConfig))
    yield engine
    engine.dispose()


def event_row(title, *, now=None, **overrides):
    now = now or datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    fields = dict(title=title, summary="隔离验收原文", impact_scope="other", importance_level=3,
                  affected_market="A股/期货", affected_sectors="solar_pv", sector_tag_source="auto",
                  sentiment="positive", source="test", is_manual=0, published_at=now,
                  expires_at=now+timedelta(days=7), created_at=now, updated_at=now)
    fields.update(overrides)
    return MarketEvent(**fields)


def seed_pagination(engine):
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    rows = [event_row(f"验收消息-{i:04d}", now=now, published_at=now-timedelta(minutes=i),
                     sentiment="negative" if i % 2 else "positive") for i in range(205)]
    expired = [event_row(f"已过期-{i:03d}", now=now, importance_level=5, expires_at=now-timedelta(seconds=1)) for i in range(12)]
    with Session(engine) as db:
        db.add_all([*rows, *expired])
        db.flush()
        ids = {row.title: row.id for row in rows}
        db.commit()
    return ids


@pytest.fixture
def ui(browser, request, case_db):
    parsed = urlparse(ORIGIN)
    assert parsed.hostname in ("127.0.0.1", "localhost") and parsed.port not in (8000, 5173)
    folder = Path(os.environ["MARKET_NEWS_EVIDENCE_DIR"]) / request.node.name
    folder.mkdir(parents=True, exist_ok=True)
    context = browser.new_context(viewport={"width": 1512, "height": 982}, timezone_id="Asia/Shanghai",
                                  locale="zh-CN", accept_downloads=True)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    # No browser request may leave the dedicated loopback origin.
    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ORIGIN + "/") else route.abort())
    page = context.new_page()
    errors, responses, requests = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: responses.append({"url": response.url, "status": response.status}) if "/api/" in response.url else None)
    page.on("request", lambda req: requests.append({"url": req.url, "method": req.method}) if "/market-events" in req.url else None)
    page.set_default_timeout(15000)
    yield page, folder, requests
    try:
        page.screenshot(path=str(folder / "final.png"), full_page=False)
        (folder / "http.json").write_text(json.dumps({"responses": responses, "market_requests": requests,
            "page_errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
        assert not errors, errors
        assert not [response for response in responses if response["status"] >= 500], responses
    finally:
        context.tracing.stop(path=str(folder / "trace.zip"))
        context.close()


def open_news(page, count=50):
    page.goto(ORIGIN + "/?tab=news", wait_until="domcontentloaded")
    expect(page.locator(".mn-page")).to_be_visible(timeout=30000)
    expect(page.locator(".mn-news-card")).to_have_count(count, timeout=30000)
    expect(page.locator(".mn-hero .ant-btn-loading")).to_have_count(0)


def choose(page, select, label):
    select.locator(".ant-select-selector").click()
    page.locator(".ant-select-dropdown:visible").get_by_text(label, exact=True).click()


def filter_select(page, label, value):
    item = page.locator("label.mn-filter-item").filter(has=page.locator("span", has_text=label)).first
    choose(page, item.locator(".ant-select"), value)


def download_csv(page, folder):
    with page.expect_download() as awaited:
        page.get_by_role("button", name="导出CSV").click()
    download = awaited.value
    path = folder / f"{len(list(folder.glob('*.csv'))) + 1:02d}-{download.suggested_filename}"
    download.save_as(path)
    assert download.failure() is None
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def test_pagination_real_total_and_last_page(ui, case_db):
    seed_pagination(case_db)
    page, folder, _ = ui
    open_news(page)
    expect(page.locator(".mn-footer-stats")).to_contain_text("205")
    expect(page.locator(".mn-news-card").first).to_contain_text("验收消息-0000")
    first = page.locator(".mn-title").all_text_contents()
    page.locator(".ant-pagination-item[title='2']").click()
    expect(page.locator(".mn-news-card").first).to_contain_text("验收消息-0050")
    assert not set(first).intersection(page.locator(".mn-title").all_text_contents())
    page.locator(".ant-pagination-item[title='5']").click()
    expect(page.locator(".mn-news-card")).to_have_count(5)
    expect(page.locator(".mn-news-card").last).to_contain_text("验收消息-0204")
    assert not page.locator(".mn-title", has_text="已过期-").count()
    page.locator(".mn-hero").scroll_into_view_if_needed()
    page.screenshot(path=str(folder / "last-page.png"))


def test_favorite_beyond_first_page_persists_and_exports(ui, case_db):
    ids = seed_pagination(case_db)
    page, folder, requests = ui
    open_news(page)
    page.locator(".ant-pagination-item[title='5']").click()
    card = page.locator(".mn-news-card").filter(has_text="验收消息-0204")
    card.locator(".mn-fav-toggle").click()
    assert ids["验收消息-0204"] in json.loads(page.evaluate("localStorage.getItem('mn_favorites')"))
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".mn-news-card")).to_have_count(50)
    page.get_by_role("button", name=re.compile("筛选")).click()
    filter_select(page, "收藏筛选", "⭐ 仅收藏")
    expect(page.locator(".mn-news-card")).to_have_count(1)
    expect(page.locator(".mn-news-card")).to_contain_text("验收消息-0204")
    assert any(f"event_ids={ids['验收消息-0204']}" in item["url"] for item in requests)
    rows = download_csv(page, folder)
    assert [r["title"] for r in rows] == ["验收消息-0204"]
    page.locator(".mn-fav-toggle").click()
    expect(page.locator(".mn-news-card")).to_have_count(0)
    expect(page.locator(".mn-empty")).to_be_visible()


def test_export_all_205_and_filtered_csv(ui, case_db):
    seed_pagination(case_db)
    page, folder, requests = ui
    open_news(page)
    rows = download_csv(page, folder)
    assert len(rows) == 205 and len({r["title"] for r in rows}) == 205
    assert rows[0]["title"] == "验收消息-0000" and rows[-1]["title"] == "验收消息-0204"
    assert any("offset=200" in req["url"] for req in requests)
    page.get_by_role("button", name=re.compile("筛选")).click()
    page.get_by_placeholder("输入标题/摘要关键词").fill("验收消息-00")
    filter_select(page, "方向", "利空")
    expect(page.locator(".mn-footer-stats")).to_contain_text("50")
    rows = download_csv(page, folder)
    assert len(rows) == 50
    assert all(row["sentiment"] == "negative" and row["title"].startswith("验收消息-00") for row in rows)


def test_label_correction_persists_through_http_and_reload(ui, case_db):
    seed_pagination(case_db)
    page, _, _ = ui
    open_news(page)
    card = page.locator(".mn-news-card").filter(has_text="验收消息-0000")
    card.locator(".mn-title").click()
    card.get_by_role("button", name="修正标记").click()
    controls = card.locator(".mn-fix-row .ant-select")
    choose(page, controls.nth(0), "宏观政策")
    choose(page, controls.nth(1), "重要")
    choose(page, controls.nth(2), "利空")
    with page.expect_response(lambda response: "/market-events/" in response.url and response.request.method == "PUT") as response:
        card.get_by_role("button", name=re.compile(r"保\s*存")).click()
    assert response.value.status == 200
    body = response.value.json()
    assert (body["impact_scope"], body["importance_level"], body["sentiment"]) == ("macro_policy", 4, "negative")
    page.reload(wait_until="domcontentloaded")
    card = page.locator(".mn-news-card").filter(has_text="验收消息-0000")
    expect(card.locator(".mn-card-header")).to_contain_text("宏观政策")
    expect(card.locator(".mn-card-header")).to_contain_text("重要")
    expect(card.locator(".mn-card-header")).to_contain_text("利空")


def test_legacy_custom_label_can_return_to_auto_via_ui(ui, case_db):
    with Session(case_db) as db:
        db.add(event_row("光伏历史标签验收", affected_sectors="custom_review", sector_tag_source=None))
        db.commit()
    page, _, _ = ui
    open_news(page, 1)
    page.locator(".mn-title").click()
    expect(page.locator(".mn-keywords")).to_contain_text("custom_review")
    expect(page.locator(".mn-keywords .ant-tag")).to_have_css("background-color", "rgb(100, 116, 139)")
    expect(page.locator(".mn-meta-footer").get_by_text("test", exact=True)).to_be_visible()
    expect(page.get_by_text("历史标签（来源未记录，保留原值）")).to_be_visible()
    with page.expect_response(lambda response: "/market-events/" in response.url and response.request.method == "PUT") as response:
        page.get_by_role("button", name="切回自动识别").click()
    assert response.value.json()["sector_tag_source"] == "auto"
    assert response.value.json()["affected_sectors"] == "solar_pv"
    expect(page.get_by_text("自动识别板块", exact=True)).to_be_visible()
    expect(page.locator(".mn-keywords")).not_to_contain_text("custom_review")
    page.reload(wait_until="domcontentloaded")
    page.locator(".mn-title").click()
    expect(page.get_by_text("自动识别板块", exact=True)).to_be_visible()


def test_hotspot_real_windows_and_independence_from_empty_list(ui, case_db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(case_db) as db:
        for days, direction, level in [(2,"positive",4),(20,"positive",3),(60,"negative",5),(120,"neutral",2),(300,"positive",3),(400,"positive",3)]:
            db.add(event_row(f"光伏历史{days}天", now=now, published_at=now-timedelta(days=days),
                             expires_at=now-timedelta(days=1), sentiment=direction, importance_level=level))
        db.add(event_row("当前列表事件无板块", now=now, affected_sectors=""))
        db.commit()
    page, folder, _ = ui
    open_news(page, 1)
    panel = page.locator(".mn-concept-panel")
    for label, count in [("近7天",1),("近一月",2),("近一季",2),("近一年",3)]:
        panel.get_by_role("button", name=label, exact=True).click()
        expect(panel.locator(".mn-sector-count")).to_have_text(f"{count}条")
    data = page.request.get(ORIGIN + "/api/v1/market-events/hotspots?days=365").json()["sectors"][0]
    assert (data["bull"],data["bear"],data["neutral"],data["total"],data["max_level"]) == (3,1,1,5,5)
    page.get_by_role("button", name=re.compile("筛选")).click()
    page.get_by_placeholder("输入标题/摘要关键词").fill("不存在的验收关键词")
    expect(page.locator(".mn-empty")).to_be_visible()
    expect(panel.locator(".mn-sector-count")).to_have_text("3条")
    panel.get_by_role("button", name="利空板块", exact=False).click()
    expect(panel.locator(".mn-sector-count")).to_have_text("1条")
    page.screenshot(path=str(folder / "year-hotspots-empty-list.png"))


def test_hotspot_failure_fallback_recomputes_window(ui, case_db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(case_db) as db:
        db.add_all(event_row(f"光伏降级样本{i}", now=now, published_at=now-timedelta(days=2 if i == 0 else 20)) for i in range(6))
        db.commit()
    page, _, _ = ui
    # Intentional transport failure only. List data still comes from the real API/MySQL.
    page.route("**/api/v1/market-events/hotspots?*", lambda route: route.abort("failed"))
    open_news(page, 6)
    panel = page.locator(".mn-concept-panel")
    expect(panel.locator(".mn-panel-footer-hint")).to_contain_text("仅基于当前已加载的 6 条消息")
    panel.get_by_role("button", name="近7天", exact=True).click()
    expect(panel.locator(".mn-sector-count")).to_have_text("1条")
    panel.get_by_role("button", name="近一月", exact=True).click()
    expect(panel.locator(".mn-sector-count")).to_have_text("6条")


def test_csv_real_browser_bom_escaping_and_formula_text(ui, case_db):
    with Session(case_db) as db:
        db.add(event_row('=SUM(1,2)"中文"', summary="摘要首行\n摘要次行"))
        db.commit()
    page, folder, _ = ui
    open_news(page, 1)
    rows = download_csv(page, folder)
    assert len(rows) == 1 and rows[0]["title"] == '\'=SUM(1,2)"中文"'
    assert rows[0]["summary"] == "摘要首行 摘要次行"


def test_utc_publication_time_displays_in_shanghai_timezone(ui, case_db):
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    with Session(case_db) as db:
        db.add(event_row("UTC时间显示验收", now=now))
        db.commit()
    page, _, _ = ui
    open_news(page, 1)
    expected = page.evaluate("iso => new Date(iso + 'Z').toLocaleDateString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})", now.isoformat())
    expect(page.locator(".mn-card-header .ant-typography")).to_have_text(expected)
