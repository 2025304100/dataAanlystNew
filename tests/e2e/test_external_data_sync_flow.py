"""E2E 测试 - 外部数据同步流程（P2-2）。

完整链路验证外部数据同步：
- 设置 Tab → 外部数据同步区域 → source Select（3 选项）→ 3 个同步按钮。

E2E 测试需要前后端同时运行：
    cd d:/ai_project/dataAanlystNew && python -m uvicorn app.main:app --port 8000
    cd d:/ai_project/dataAanlystNew/frontend && npm run dev
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# i18n 文本（zh-CN / en）。
SETTINGS_TAB_LABELS = ("设置", "Settings")
EXTERNAL_NAV_LABELS = ("外部数据", "External Data")
EXTERNAL_SECTION_TITLE_LABELS = ("外部数据同步", "External Data Sync")
SOURCE_WATCHLIST_LABELS = ("自选股", "Watchlist")
SOURCE_POSITIONS_LABELS = ("持仓", "Positions")
SOURCE_ALL_LABELS = ("全部", "All")
SYNC_FUNDAMENTAL_LABELS = ("同步股票估值", "Sync Stock Valuation")
SYNC_CAPITAL_FLOW_LABELS = ("同步资金流", "Sync Capital Flow")
SYNC_ETF_LABELS = ("同步 ETF 指标", "Sync ETF Indicators")

EXPECTED_SOURCE_OPTION_COUNT = 3
EXPECTED_SYNC_BUTTON_COUNT = 3


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


def _goto_external_data_sync(page):
    """切换到 设置 → 外部数据。"""
    _click_view_tab(page, SETTINGS_TAB_LABELS)
    _click_settings_nav(page, EXTERNAL_NAV_LABELS)
    page.wait_for_timeout(2000)


def test_external_data_sync_section_visible(page):
    """【P2-2 E2E】外部数据同步区域可见。"""
    _goto_external_data_sync(page)

    # external 内容容器可见
    content = page.locator("[data-settings-content='settings-external']").first
    assert content.is_visible(), "未找到外部数据同步内容容器 settings-external"

    # 区域根容器
    section = page.locator(".external-data-sync-section").first
    assert section.is_visible(), "未找到外部数据同步根容器 .external-data-sync-section"

    # Card 标题含「外部数据同步」
    title = None
    for label in EXTERNAL_SECTION_TITLE_LABELS:
        candidate = page.locator(
            f".external-data-sync-section .ant-card-head-title:has-text('{label}')"
        ).first
        if candidate.is_visible():
            title = candidate
            break
    assert title is not None, "未找到外部数据同步 Card 标题「外部数据同步」"


def test_source_select_has_three_options(page):
    """【P2-2 E2E】source Select 含 3 选项（自选股/持仓/全部）。"""
    _goto_external_data_sync(page)

    # 找到 source Select（区域内的 Select）
    source_select = page.locator(".external-data-sync-section .ant-select").first
    assert source_select.is_visible(), "未找到 source Select"
    # 展开下拉
    source_select.click()
    page.wait_for_timeout(800)

    # 下拉选项容器
    options = page.locator(".ant-select-item-option")
    count = options.count()
    assert count == EXPECTED_SOURCE_OPTION_COUNT, (
        f"source Select 选项数应为 {EXPECTED_SOURCE_OPTION_COUNT}，实际为 {count}"
    )

    # 验证三个选项文本（至少匹配 zh-CN 文本）
    option_texts = []
    for i in range(count):
        option_texts.append(options.nth(i).inner_text())
    all_labels = (
        SOURCE_WATCHLIST_LABELS + SOURCE_POSITIONS_LABELS + SOURCE_ALL_LABELS
    )
    matched = sum(1 for txt in option_texts if any(label in txt for label in all_labels))
    assert matched == EXPECTED_SOURCE_OPTION_COUNT, (
        f"source Select 选项未匹配自选股/持仓/全部，实际选项文本: {option_texts}"
    )

    # 关闭下拉（点击空白区域）
    page.locator("body").click(position={"x": 0, "y": 0})
    page.wait_for_timeout(500)


def test_sync_buttons_visible(page):
    """【P2-2 E2E】3 个同步按钮可见。

    期望按钮：同步股票估值 / 同步资金流 / 同步 ETF 指标。
    """
    _goto_external_data_sync(page)

    section = page.locator(".external-data-sync-section").first
    assert section.is_visible(), "外部数据同步区域不可见"

    # 验证三个同步按钮分别可见
    button_groups = [
        ("同步股票估值", SYNC_FUNDAMENTAL_LABELS),
        ("同步资金流", SYNC_CAPITAL_FLOW_LABELS),
        ("同步 ETF 指标", SYNC_ETF_LABELS),
    ]
    visible_count = 0
    for _, labels in button_groups:
        for label in labels:
            btn = section.locator(f"button:has-text('{label}')").first
            if btn.is_visible():
                visible_count += 1
                break

    assert visible_count == EXPECTED_SYNC_BUTTON_COUNT, (
        f"应可见 {EXPECTED_SYNC_BUTTON_COUNT} 个同步按钮，实际匹配 {visible_count} 个"
    )
