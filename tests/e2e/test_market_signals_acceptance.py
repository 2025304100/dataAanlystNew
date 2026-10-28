"""Chromium -> production UI -> public APIs -> isolated MySQL and real outbox."""
import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not os.environ.get("MARKET_SIGNALS_CLOCK_FILE"),
    reason="Run scripts/verify_market_signals_acceptance.py")]
ORIGIN = os.environ.get("E2E_FRONTEND_URL", "")


@pytest.fixture
def ui(browser, request):
    folder = Path(os.environ["MARKET_NEWS_EVIDENCE_DIR"])/request.node.name
    folder.mkdir(parents=True, exist_ok=True)
    Path(os.environ["MARKET_SIGNALS_CLOCK_FILE"]).write_text("2026-10-27T09:10:00", encoding="utf-8")
    context = browser.new_context(viewport={"width":1512,"height":982}, locale="zh-CN", timezone_id="Asia/Shanghai")
    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(ORIGIN+"/") else route.abort())
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    page.set_default_timeout(20000)
    errors, responses = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: responses.append({"url":response.url,"status":response.status}) if "/api/" in response.url else None)
    yield page, folder
    try:
        page.screenshot(path=str(folder/"final.png"))
        (folder/"http.json").write_text(json.dumps({"responses":responses,"page_errors":errors},ensure_ascii=False,indent=2),encoding="utf-8")
        assert not errors, errors
        assert not [row for row in responses if row["status"]>=500], responses
    finally:
        context.tracing.stop(path=str(folder/"trace.zip"))
        context.close()


def open_center(page):
    page.goto(ORIGIN+"/?tab=news", wait_until="domcontentloaded")
    page.get_by_role("button", name="行情异动提醒", exact=True).click()
    expect(page.locator(".msc-page")).to_be_visible()
    expect(page.locator(".msc-page .ant-spin-spinning")).to_have_count(0)
    page.get_by_role("tab",name="关注设置",exact=True).click()


def add_rule(page, name, sector=False):
    page.get_by_role("button", name="新增关注", exact=True).click()
    dialog = page.get_by_role("dialog", name="新增关注", exact=True)
    dialog.get_by_label("规则名称", exact=True).fill(name)
    if sector:
        dialog.get_by_label("关注类型", exact=True).select_option("sector")
        dialog.get_by_label("关注板块", exact=True).select_option("semiconductor")
        dialog.get_by_label("验证行情代码", exact=True).fill("000300")
    else:
        dialog.get_by_label("关注股票", exact=True).select_option("000001")
    dialog.get_by_label("观察窗口", exact=True).select_option("3" if sector else "1")
    dialog.get_by_label("最低波动幅度（%）", exact=True).fill("3")
    dialog.get_by_label("按历史波动提高门槛", exact=True).uncheck()
    if not sector:
        dialog.get_by_label("运行模式", exact=True).select_option("trial")
    dialog.get_by_role("button", name="保存关注", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("cell", name=re.compile("^"+re.escape(name)))).to_be_visible()


def wait_job(page):
    expect(page.locator(".msc-page").get_by_text("异动任务完成", exact=True)).to_be_visible(timeout=40000)


def test_complete_stock_flow_delivery_dedup_outcome_and_notification_navigation(ui):
    page, folder = ui
    open_center(page)
    add_rule(page,"浏览器银行一日观察")
    page.get_by_role("button", name="运行设置", exact=True).click()
    settings = page.get_by_role("dialog",name="运行设置",exact=True)
    settings.get_by_label("AI分析配置",exact=True).select_option(label="隔离验收模型 (fixture-model)")
    settings.get_by_label("启用自动运行",exact=True).check()
    settings.get_by_role("button",name="保存设置",exact=True).click()
    expect(settings).not_to_be_visible()
    page.get_by_role("tab",name="消息与分析",exact=True).click()
    page.get_by_role("button",name="补充原文证据",exact=True).click()
    evidence = page.get_by_role("dialog",name="补充原文证据",exact=True)
    evidence.get_by_label("消息标题",exact=True).fill("验收银行新增订单正式实施")
    evidence.get_by_label("原文内容",exact=True).fill("验收银行获得新增订单100亿元，项目即日起执行。")
    evidence.get_by_label("来源链接",exact=True).fill("https://www.gov.cn/fixture-order")
    evidence.get_by_label("发布时间（北京时间）",exact=True).fill("2026-10-27T17:00")
    evidence.get_by_role("button",name="保存证据",exact=True).click()
    expect(evidence).not_to_be_visible()
    page.get_by_role("button",name="分析 验收银行新增订单正式实施",exact=True).click()
    wait_job(page)
    expect(page.get_by_text("评分: 85",exact=True)).to_be_visible()
    page.get_by_role("button",name="分析并运行",exact=True).click()
    wait_job(page)
    page.get_by_role("tab",name="提醒与验证",exact=True).click()
    expect(page.get_by_text("验证中",exact=True)).to_be_visible()
    rows = page.request.get(ORIGIN+"/api/v1/market-signals/predictions").json()
    assert rows["total"]==1 and rows["items"][0]["delivery_status"]=="sent"
    page.get_by_role("button",name="分析并运行",exact=True).click()
    wait_job(page)
    assert page.request.get(ORIGIN+"/api/v1/market-signals/predictions").json()["total"]==1
    # A later, externally supplied clock closes the frozen observation window.
    Path(os.environ["MARKET_SIGNALS_CLOCK_FILE"]).write_text("2026-10-29T09:10:00",encoding="utf-8")
    page.get_by_role("button",name="分析并运行",exact=True).click()
    wait_job(page)
    expect(page.get_by_text("未命中",exact=True)).to_be_visible()
    page.screenshot(path=str(folder/"settled.png"))
    result = page.request.get(ORIGIN+"/api/v1/market-signals/dashboard").json()
    assert result["live"]["misses"]==1 and result["live"]["precision"]==0
    assert result["live"]["validation_status"]=="not_verified"
    page.locator(".ant-drawer-close").click()
    page.get_by_role("button",name="消息通知",exact=True).click()
    page.get_by_text("行情异动关注：验收银行新增订单正式实施",exact=True).click()
    expect(page.locator(".msc-page")).to_be_visible()


def test_sector_subscription_and_rule_revision_keep_stock_settings_independent(ui):
    page, folder = ui
    open_center(page)
    add_rule(page,"浏览器半导体三日观察",sector=True)
    row = page.get_by_role("row").filter(has=page.get_by_role("cell",name="浏览器半导体三日观察",exact=False))
    row.get_by_role("button",name="暂停 浏览器半导体三日观察",exact=True).click()
    expect(row.get_by_role("button",name="启用 浏览器半导体三日观察",exact=True)).to_be_visible()
    rules = page.request.get(ORIGIN+"/api/v1/market-signals/rules").json()
    sector = next(item for item in rules if item["name"]=="浏览器半导体三日观察")
    assert sector["window_days"]==3 and not sector["enabled"] and sector["revision"]==2
    stock = next(item for item in rules if item["name"]=="浏览器银行一日观察")
    assert stock["window_days"]==1 and stock["enabled"]
    row.get_by_role("button",name="查看版本",exact=True).click()
    expect(page.get_by_role("dialog",name="浏览器半导体三日观察 · 版本",exact=True)).to_be_visible()
    page.screenshot(path=str(folder/"versions.png"))
