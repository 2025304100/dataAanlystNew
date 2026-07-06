"""E2E 测试 - 接口管理流程（P2-2）。

完整链路验证接口管理：
- 设置 Tab → 接口管理区域 → 接口列表（18 行）→ 单接口探测 → 批量探测。

E2E 测试需要前后端同时运行：
    cd d:/ai_project/dataAanlystNew && python -m uvicorn app.main:app --port 8000
    cd d:/ai_project/dataAanlystNew/frontend && npm run dev
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# i18n 文本（zh-CN / en）。
SETTINGS_TAB_LABELS = ("设置", "Settings")
API_MGMT_NAV_LABELS = ("接口管理", "API Management")
API_MGMT_SECTION_TITLE_LABELS = ("第三方接口状态", "Third-party API Status")
REFRESH_LABELS = ("刷新", "Refresh")
PROBE_ALL_LABELS = ("批量探测", "Probe All")
PROBE_LABELS = ("探测", "Probe")

# AKSHARE_API_REGISTRY 中预置 18 条接口元数据
EXPECTED_API_ROW_COUNT = 18


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


def _click_settings_nav(page, labels):
    """点击设置页左侧 nav 切换到指定 section。"""
    for label in labels:
        nav = page.locator(f".settings-nav-item:has-text('{label}')").first
        if nav.is_visible():
            nav.click()
            page.wait_for_timeout(1500)
            page.wait_for_load_state("networkidle")
            return nav
    return None


def _goto_api_mgmt(page):
    """切换到 设置 → 接口管理。"""
    _click_view_tab(page, SETTINGS_TAB_LABELS)
    _click_settings_nav(page, API_MGMT_NAV_LABELS)
    # 等待接口列表加载
    page.wait_for_timeout(2000)


def test_api_mgmt_tab_visible(page):
    """【P2-2 E2E】切换到「设置」Tab，接口管理区域可见。"""
    _goto_api_mgmt(page)

    # api-mgmt 内容容器可见
    content = page.locator("[data-settings-content='settings-api-mgmt']").first
    assert content.is_visible(), "未找到接口管理内容容器 settings-api-mgmt"

    # Card 标题含「第三方接口状态」
    title = None
    for label in API_MGMT_SECTION_TITLE_LABELS:
        candidate = page.locator(f".ant-card-head-title:has-text('{label}')").first
        if candidate.is_visible():
            title = candidate
            break
    assert title is not None, "未找到接口管理 Card 标题「第三方接口状态」"


def test_api_list_loaded(page):
    """【P2-2 E2E】接口列表加载（18 行）。

    AKSHARE_API_REGISTRY 预置 18 条接口元数据。
    """
    _goto_api_mgmt(page)

    # 等待表格行加载
    rows = page.locator("[data-settings-content='settings-api-mgmt'] .ant-table-row")
    # 给表格加载留出时间
    page.wait_for_timeout(1000)
    count = rows.count()
    assert count == EXPECTED_API_ROW_COUNT, (
        f"接口列表行数应为 {EXPECTED_API_ROW_COUNT}，实际为 {count}"
    )


def test_single_probe_button_clickable(page):
    """【P2-2 E2E】单接口探测按钮可点击。

    每行操作列含「探测」按钮（apiMgmtProbe）。
    """
    _goto_api_mgmt(page)
    page.wait_for_timeout(1000)

    rows = page.locator("[data-settings-content='settings-api-mgmt'] .ant-table-row")
    if rows.count() == 0:
        pytest.skip("接口列表未加载，跳过单接口探测按钮验证")

    # 第一行内的「探测」按钮
    first_row = rows.first
    probe_btn = None
    for label in PROBE_LABELS:
        candidate = first_row.locator(f"button:has-text('{label}')").first
        if candidate.is_visible():
            probe_btn = candidate
            break
    assert probe_btn is not None, "未在第一行找到「探测」按钮"
    # 可点击（enabled）即视为通过；不实际触发，避免触发真实网络请求
    assert probe_btn.is_enabled(), "「探测」按钮不可点击"


def test_batch_probe_button_clickable(page):
    """【P2-2 E2E】批量探测按钮可点击。

    Card extra 区域含「批量探测」按钮（apiMgmtProbeAll）。
    """
    _goto_api_mgmt(page)

    # Card extra 内的「批量探测」按钮
    probe_all_btn = None
    for label in PROBE_ALL_LABELS:
        candidate = page.locator(
            f"[data-settings-content='settings-api-mgmt'] button:has-text('{label}')"
        ).first
        if candidate.is_visible():
            probe_all_btn = candidate
            break
    assert probe_all_btn is not None, "未找到「批量探测」按钮"
    assert probe_all_btn.is_enabled(), "「批量探测」按钮不可点击"
