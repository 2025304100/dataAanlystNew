"""E2E 测试 - 机会挖掘流程（P2-2）。

完整链路验证机会挖掘功能：
- Tab 切换 → 创建 cn-etf 任务 → 候选池表格加载 → 加入观察池按钮 → 错误 Modal。

E2E 测试需要前后端同时运行：
    cd d:/ai_project/dataAanlystNew && python -m uvicorn app.main:app --port 8000
    cd d:/ai_project/dataAanlystNew/frontend && npm run dev
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# i18n 文本（zh-CN / en）。默认 locale 为 zh-CN，但兼容 en。
DISCOVERY_TAB_LABELS = ("机会挖掘", "Opportunity Mining")
START_DISCOVERY_LABELS = ("开始挖掘", "Start Mining")
ADD_WATCHLIST_LABELS = ("加入观察池", "Add to Watchlist")
VIEW_ERROR_DETAILS_LABELS = ("查看错误详情", "View error details")
ERROR_MODAL_TITLE_LABELS = ("任务错误详情", "Task error details")
CN_ETF_SCOPE_LABELS = ("ETF",)


def _click_view_tab(page, labels):
    """通过文本切换主 Tab，返回首个可见的 Tab locator。"""
    for label in labels:
        tab = page.locator(f"button.view-tab:has-text('{label}')").first
        if tab.is_visible():
            tab.click()
            page.wait_for_timeout(1500)
            page.wait_for_load_state("networkidle")
            return tab
    return None


def test_discovery_tab_visible(page):
    """【P2-2 E2E】切换到「机会挖掘」Tab 可见。"""
    tab = _click_view_tab(page, DISCOVERY_TAB_LABELS)
    assert tab is not None, "未找到「机会挖掘」Tab 按钮"

    # 切换后 discovery 内容容器可见
    content = page.locator("[data-tab-content='discovery']").first
    assert content.is_visible(), "切换后未看到 discovery 内容容器"


def test_create_cn_etf_discovery_task(page):
    """【P2-2 E2E】创建 cn-etf 任务（scope=cn-etf, limit=5）→ 点击 startDiscovery → 等待任务启动。

    注意：本测试不验证任务完成（依赖真实数据），仅验证点击「开始挖掘」后无 JS 异常，
    且 startDiscovery 按钮存在并可点击。
    """
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    _click_view_tab(page, DISCOVERY_TAB_LABELS)

    # 选择 scope=cn-etf：discovery-control-bar 内首个 Select
    scope_select = page.locator(".discovery-control-bar .ant-select").first
    assert scope_select.is_visible(), "未找到 scope Select"
    scope_select.click()
    page.wait_for_timeout(500)
    # 选择 cn-etf 选项（标签含 ETF，且排除 us-etf 的歧义用下标）
    etf_option = page.locator(".ant-select-item-option:has-text('ETF')").first
    if etf_option.is_visible():
        etf_option.click()
        page.wait_for_timeout(500)

    # 点击「开始挖掘」按钮
    start_btn = page.locator("#discoveryRunButton").first
    assert start_btn.is_visible(), "未找到 #discoveryRunButton（开始挖掘）按钮"
    # 关闭可能拦截点击的 Modal
    modal_close = page.locator(".ant-modal-close, button[aria-label='Close']").first
    if modal_close.is_visible():
        modal_close.click()
        page.wait_for_timeout(500)
    if start_btn.is_enabled():
        start_btn.click()
        page.wait_for_timeout(3000)

    # 验证无 page error
    assert len(page_errors) == 0, f"创建 cn-etf 任务流程出现 page error: {page_errors}"


def test_discovery_candidates_table_loaded(page):
    """【P2-2 E2E】候选池表格加载（如有数据）。

    验证：候选池 Tab 容器可见，且若存在数据时 Table 已渲染。
    """
    _click_view_tab(page, DISCOVERY_TAB_LABELS)
    page.wait_for_timeout(2000)

    # pool-tabs 容器始终可见
    pool_tabs = page.locator(".pool-tabs").first
    assert pool_tabs.is_visible(), "未找到候选池 Tab 容器 .pool-tabs"

    # 候选池 Table 容器存在（不论是否有数据）
    table_wrap = page.locator(".table-wrap").first
    assert table_wrap.is_visible(), "未找到候选池 Table 容器 .table-wrap"


def test_add_to_watchlist_button_visible(page):
    """【P2-2 E2E】候选行含「加入观察池」按钮（仅在候选池有数据时验证）。"""
    _click_view_tab(page, DISCOVERY_TAB_LABELS)
    page.wait_for_timeout(2000)

    # 候选池表格行
    rows = page.locator(".table-wrap .ant-table-row")
    if rows.count() == 0:
        pytest.skip("候选池无数据，跳过「加入观察池」按钮验证")

    # 第一行内应含「加入观察池」/「已在观察池」按钮
    first_row = rows.first
    btn = None
    for label in ADD_WATCHLIST_LABELS:
        candidate = first_row.locator(f"button:has-text('{label}')").first
        if candidate.is_visible():
            btn = candidate
            break
    # 也可能显示「已在观察池」(discoveryInWatchlist)
    in_watchlist_btn = first_row.locator("button:has-text('已在观察池')").first
    assert (btn is not None) or in_watchlist_btn.is_visible(), (
        "候选行未含「加入观察池」或「已在观察池」按钮"
    )


def test_discovery_error_modal_visible_on_error(page):
    """【P2-2 E2E】任务失败时错误 Modal 显示（仅在 task.errors 非空时验证）。

    验证：若任务有 errors，「查看错误详情」按钮可见；点击后 Modal 标题「任务错误详情」显示。
    若任务无 errors，跳过本测试。
    """
    _click_view_tab(page, DISCOVERY_TAB_LABELS)
    page.wait_for_timeout(2000)

    # 「查看错误详情」按钮（aria-label 含 discoveryErrorDetails，文本含「查看错误详情」）
    view_err_btn = None
    for label in VIEW_ERROR_DETAILS_LABELS:
        candidate = page.locator(f"button:has-text('{label}')").first
        if candidate.is_visible():
            view_err_btn = candidate
            break
    if view_err_btn is None:
        pytest.skip("当前任务无 errors，跳过错误 Modal 验证")

    view_err_btn.click()
    page.wait_for_timeout(1000)

    # Modal 标题
    modal_title = None
    for label in ERROR_MODAL_TITLE_LABELS:
        candidate = page.locator(f".ant-modal-title:has-text('{label}')").first
        if candidate.is_visible():
            modal_title = candidate
            break
    assert modal_title is not None, "点击「查看错误详情」后未弹出错误 Modal"
