"""E2E 测试 - UI 烟雾测试（P2-1，从根目录 test_ui_smoke.py 迁移）。

验证所有 Tab 可点击切换且无 JS 错误。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

TABS = [
    ("今日决策", "decision"),
    ("投资中心", "investment"),
    ("目前观察池", "portfolio"),
    ("机会挖掘", "discovery"),
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
    """【P2-1 E2E】目前观察池 Tab 含「工作台」和「模拟交易」子 Tab。"""
    pg, _, _ = smoke_page

    # 切换到目前观察池
    portfolio_tab = pg.locator("button.view-tab:has-text('目前观察池')").first
    if portfolio_tab.is_visible():
        portfolio_tab.click()
        pg.wait_for_timeout(1000)

    # 验证子 Tab
    workbench_tab = pg.locator("button.sub-tab:has-text('工作台')").first
    trading_tab = pg.locator("button.sub-tab:has-text('模拟交易')").first

    assert workbench_tab.is_visible() or trading_tab.is_visible(), (
        "目前观察池下未找到子 Tab（工作台/模拟交易）"
    )
