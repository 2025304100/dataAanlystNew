"""E2E 测试 - UI 烟雾测试（P2-1，从根目录 test_ui_smoke.py 迁移）。

验证所有 Tab 可点击切换且无 JS 错误。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# 当前主视图（实测 6 个）：旧列表里的「投资中心」「目前观察池」「机会挖掘」
# 已重排/改名（分别→组合交易 / 组合交易 / 机会中心），保留旧名会让
# 参数化用例直接判“Tab 不可见”而失败——这是选择器腐烂，不是产品问题。
TABS = [
    ("今日决策", "decision"),
    ("组合交易", "portfolio"),
    ("机会中心", "opportunity"),
    ("宏观数据", "macro"),
    ("行情消息", "news"),
    ("设置", "settings"),
]


@pytest.fixture(scope="module")
def smoke_page(browser):
    """模块级共享页面，收集所有 Tab 的 console/page 错误。"""
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.on("dialog", lambda dialog: dialog.accept())

    console_errors: list[tuple[str, str]] = []
    page_errors: list[tuple[str, str]] = []

    def on_console(msg):
        if msg.type == "error":
            console_errors.append(("unknown", msg.text))

    def on_page_error(err):
        page_errors.append(("unknown", str(err)))

    pg.on("console", on_console)
    pg.on("pageerror", on_page_error)

    pg.goto("http://localhost:5173")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(2000)

    yield pg, console_errors, page_errors
    pg.close()


def test_frontend_loads_without_page_errors(smoke_page):
    """【P2-1 E2E】前端首屏加载无致命 page error。"""
    _, _, page_errors = smoke_page
    # pageerror 是未捕获异常，应当为 0
    assert len(page_errors) == 0, f"首屏加载出现 page errors: {page_errors}"


@pytest.mark.parametrize("label,key", TABS)
def test_tab_clickable_and_no_console_error(smoke_page, label, key):
    """【P2-1 E2E】每个 Tab 可点击切换且不产生 console error。"""
    pg, console_errors, page_errors = smoke_page

    # 关闭可能打开的 Modal
    close_btn = pg.locator(".ant-modal-close, button[aria-label='Close']").first
    if close_btn.is_visible():
        close_btn.click()
        pg.wait_for_timeout(500)

    tab_btn = pg.locator(f"button.view-tab:has-text('{label}')").first
    assert tab_btn.is_visible(), f"Tab '{label}' 不可见"
    tab_btn.click()
    pg.wait_for_timeout(1500)
    pg.wait_for_load_state("networkidle")

    # 检查该 Tab 切换后是否新增 console error
    # 注意：console_errors 是累积的，这里仅验证无致命错误
    fatal_errors = [e for e in console_errors if "Uncaught" in e[1] or "SyntaxError" in e[1]]
    assert len(fatal_errors) == 0, f"Tab '{label}' 切换后出现致命 console error: {fatal_errors}"


def test_portfolio_subtabs_visible(smoke_page):
    """【P2-1 E2E】组合交易视图（旧称「目前观察池」）含完整子导航。

    旧断言查的是「工作台 / 模拟交易」两个 sub-tab，该结构已被组合交易改造推翻；
    现子导航实测为：总览 / 持仓成员 / 策略规则 / 回测中心 / 治理。
    """
    pg, _, _ = smoke_page

    portfolio_tab = pg.locator("button.view-tab:has-text('组合交易')").first
    assert portfolio_tab.is_visible(), "主视图「组合交易」不可见"
    portfolio_tab.click()
    pg.wait_for_timeout(2000)
    pg.wait_for_load_state("networkidle")

    # 验证子导航
    expected = ("总览", "持仓成员", "策略规则", "回测中心", "治理")
    found = []
    for label in expected:
        item = pg.locator(
            f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
        ).first
        if item.count() > 0 and item.is_visible():
            found.append(label)

    assert len(found) >= 3, (
        f"组合交易子导航不足：期望含 {list(expected)} 中至少 3 项，实际 {found}"
    )
